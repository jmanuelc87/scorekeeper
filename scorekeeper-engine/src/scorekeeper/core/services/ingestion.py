"""Parse uploaded conversations and persist them as an ingested run.

Ingestion is deliberately separate from scoring so that starting a run is
decoupled from the upload and can run off the request path: this module only
persists the ``BenchmarkRun -> PlatformExecution -> ScenarioResult -> Turn``
tree at status ``ingerido`` (*persisted, not started*) and returns the run id.
It never starts scoring — see :mod:`scorekeeper.core.services.runs`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.core.importer import normalize_messages, parse_conversation
from scorekeeper.core.metrics.selection import DEFAULT_USE_CASE, sync_metrics, sync_prompts
from scorekeeper.core.services.status import STATUS_INGERIDO
from scorekeeper.db.connection import session_scope
from scorekeeper.db.models import (
    BenchmarkRun,
    PlatformExecution,
    ScenarioResult,
    SourceFile,
    Turn,
)
from scorekeeper.db.repositories import use_cases as use_case_repo

logger = logging.getLogger(__name__)


class UnknownUseCaseError(ValueError):
    """An upload named a ``use_case`` that does not exist.

    A ``ValueError`` subclass so the existing rollback path still catches it, but its
    own type so the HTTP layer answers 422 (unprocessable input) rather than the 400 it
    gives a malformed sheet. Create the use case first with ``POST /use-cases``.
    """


@dataclass
class UploadedFile:
    """One uploaded conversation file plus its resolved evaluation metadata.

    ``scenario_id`` and ``use_case`` are resolved by the caller (e.g. the HTTP
    endpoint applies per-file overrides and defaults) before reaching the
    orchestrator. ``platform`` is an optional per-file override; when ``None`` the
    file falls back to the run-level platform passed to :func:`ingest_evaluation`.

    ``messages`` carries an already-extracted conversation (the browser extension
    scrapes turns straight off a chat UI, so there is no spreadsheet to parse). When
    set, ``content`` is not parsed and only feeds the ``SourceFile`` hash — pass the
    serialized capture so the provenance hash still identifies the input.

    ``model_name`` is the model that produced the responses, when the client knows it;
    only a browser capture ever does, so the spreadsheet path leaves it ``None``.
    """

    filename: str
    content: bytes
    scenario_id: str
    use_case: str = DEFAULT_USE_CASE
    platform: str | None = None
    messages: list[dict[str, Any]] | None = None
    model_name: str | None = None


def project_turns(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project parsed messages into evaluation-turn dicts.

    ``parse_conversation`` yields one dict per message tagged with a ``turn``
    number (a user+model pair shares a number). This groups by that number and,
    per turn, joins the ``user`` messages into ``prompt`` and the ``model``
    messages into ``response`` (a missing side becomes ``""``), carrying through
    the raw ``retrieved_context_source`` cell / ``expected_output`` when the sheet
    provided them. Turns are returned in ascending ``turn_number`` order.
    """
    grouped: OrderedDict[int, dict[str, Any]] = OrderedDict()
    for message in messages:
        turn = message.get("turn", 1)
        bucket = grouped.setdefault(
            turn,
            {"prompt": [], "response": [], "retrieved_context_source": "", "expected_output": ""},
        )
        role = message.get("role")
        content = message.get("content", "")
        if role == "user":
            bucket["prompt"].append(content)
        elif role == "model":
            bucket["response"].append(content)
        # Keep the first non-empty raw context cell and expected output seen for the turn.
        source = message.get("retrieved_context_source")
        if source and not bucket["retrieved_context_source"]:
            bucket["retrieved_context_source"] = source
        expected = message.get("expected_output")
        if expected and not bucket["expected_output"]:
            bucket["expected_output"] = expected

    turns: list[dict[str, Any]] = []
    for turn_number in sorted(grouped):
        bucket = grouped[turn_number]
        turns.append(
            {
                "turn_number": turn_number,
                "prompt": "\n".join(bucket["prompt"]),
                "response": "\n".join(bucket["response"]),
                "retrieved_context_source": bucket["retrieved_context_source"] or None,
                "expected_output": bucket["expected_output"] or None,
            }
        )
    return turns


async def ingest_evaluation(
    platform: str,
    files: list[UploadedFile],
    *,
    session: AsyncSession | None = None,
) -> str:
    """Parse ``files`` and persist a queued run for ``platform``; return its ``run_id``.

    Builds one ``BenchmarkRun`` and, under it, one ``PlatformExecution`` per distinct
    platform — each file lands under ``upload.platform`` when set, otherwise under the
    run-level ``platform`` fallback. Each file becomes one ``ScenarioResult`` (with its
    ``Turn`` rows) attached to its platform's execution. The run is committed with
    status ``ingerido`` (persisted, not yet started) and left unscored; a caller later
    starts it via :func:`start_run` (which enqueues the per-turn evaluation chain).

    This is the fast, on-request half: parsing is synchronous so a malformed sheet is
    rejected here (as ``ValueError``) before anything is persisted. ``session`` defaults
    to ``SessionLocal()``; tests inject an in-memory session.

    Raises ``ValueError`` when ``platform`` or ``files`` is empty, or when a file
    cannot be parsed (propagated from ``parse_conversation``); the transaction is
    rolled back first so nothing is persisted.
    """
    if not platform:
        raise ValueError("Se requiere una plataforma.")
    if not files:
        raise ValueError("Se requiere al menos un archivo.")

    async with session_scope(session) as db:
        try:
            # Keep the metric catalog and the "default" use case in sync with the code
            # registry, then resolve every upload's use_case name to its row.
            await sync_metrics(db)
            await db.flush()
            # Prompt slots need their metric rows flushed above to point at.
            await sync_prompts(db)
            await db.flush()

            use_case_ids = await use_case_repo.use_case_ids(db)
            unknown = sorted({upload.use_case for upload in files} - set(use_case_ids))
            if unknown:
                raise UnknownUseCaseError(
                    "Caso(s) de uso desconocido(s): "
                    + ", ".join(unknown)
                    + ". Créalo con POST /use-cases."
                )

            # Parse each file once and record its provenance as a SourceFile.
            parsed: list[tuple[UploadedFile, list[dict[str, Any]], SourceFile]] = []
            for upload in files:
                messages = await _parse_upload(upload)
                logger.info(
                    "Analizado %s: %d mensaje(s) -> %d turno(s)",
                    upload.filename,
                    len(messages),
                    len(project_turns(messages)),
                )
                source_file = SourceFile(
                    filename=upload.filename,
                    file_hash=hashlib.sha256(upload.content).hexdigest(),
                )
                db.add(source_file)
                parsed.append((upload, messages, source_file))

            run = BenchmarkRun(status=STATUS_INGERIDO)
            # A run references a single source file; only meaningful with one upload.
            if len(parsed) == 1:
                run.source_file = parsed[0][2]

            # One PlatformExecution per distinct resolved platform (first-seen order);
            # each file's optional override wins over the run-level fallback.
            executions: dict[str, PlatformExecution] = {}
            for upload, messages, _ in parsed:
                resolved = upload.platform or platform
                platform_exec = executions.get(resolved)
                if platform_exec is None:
                    platform_exec = PlatformExecution(platform=resolved, run=run)
                    executions[resolved] = platform_exec
                scenario = ScenarioResult(
                    scenario_id=upload.scenario_id,
                    use_case_id=use_case_ids[upload.use_case],
                    model_name=upload.model_name,
                    source_ref=upload.filename,
                    raw_conversation={"messages": messages},
                    platform_execution=platform_exec,
                )
                for turn in project_turns(messages):
                    # Store the raw context cell; the retrieval pipeline (score-time worker)
                    # fetches/extracts it into ``retrieved_documents`` — nothing is decoupled here.
                    turn_row = Turn(
                        turn_number=turn["turn_number"],
                        prompt=turn["prompt"],
                        response=turn["response"],
                        expected_output=turn["expected_output"],
                        retrieved_context_source=turn["retrieved_context_source"],
                    )
                    scenario.turns.append(turn_row)

            db.add(run)
            # Counted from the parsed input rather than by walking the committed run tree:
            # the relationships were built in memory here, but re-reading them after the
            # commit would be a lazy load the async session cannot serve.
            turn_total = sum(len(project_turns(messages)) for _, messages, _ in parsed)
            await db.commit()
            logger.info(
                "Run %s ingerido: %d plataforma(s) × %d archivo(s) = %d turno(s).",
                run.id,
                len(executions),
                len(parsed),
                turn_total,
            )
            return str(run.id)
        except Exception:
            await db.rollback()
            raise


async def _parse_upload(upload: UploadedFile) -> list[dict[str, Any]]:
    """Write ``upload`` to a temp ``.xlsx`` and parse it into raw messages.

    ``parse_conversation`` needs a filesystem path (openpyxl opens by path), so the
    in-memory upload is spilled to a short-lived temp file that is always removed.
    An upload that already carries ``messages`` (a browser capture) skips the
    spreadsheet entirely and is only normalized.

    The spreadsheet path is blocking file + CPU work and this now runs on the event
    loop (the API's routes are coroutines), so it is offloaded to a worker thread.
    """
    if upload.messages is not None:
        messages = normalize_messages(upload.messages)
        # Expose the captured context under the same ``retrieved_context_source`` key
        # the .xlsx path uses (see ``parse_conversation``), so ``_group_turns`` stores
        # it on the turn and the retrieval pipeline can interpret it later.
        for message in messages:
            if "retrieved_context" in message:
                message["retrieved_context_source"] = message.pop("retrieved_context")
        return messages

    return await asyncio.to_thread(_parse_xlsx_bytes, upload.content)


def _parse_xlsx_bytes(content: bytes) -> list[dict[str, Any]]:
    """Spill ``content`` to a temp ``.xlsx`` and parse it (blocking; run off-loop)."""
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    try:
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(content)
        return parse_conversation(path)
    finally:
        os.unlink(path)
