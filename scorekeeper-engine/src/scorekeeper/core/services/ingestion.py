"""Parse uploaded conversations and persist them as an ingested run.

Ingestion is deliberately separate from scoring so that starting a run is
decoupled from the upload and can run off the request path: this module only
persists the ``BenchmarkRun -> PlatformExecution -> ScenarioResult -> Turn``
tree at status ``ingerido`` (*persisted, not started*) and returns the run id.
It never starts scoring — see :mod:`scorekeeper.core.services.runs`.

With ``reuse_scenarios`` the tree is not necessarily new: a still-unscored scenario
already ingested under that id takes the incoming executions instead, so a browser
capture per platform still builds one comparable scenario. See :func:`ingest_evaluation`.
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
from scorekeeper.db.repositories import runs as run_repo
from scorekeeper.db.repositories import scenarios as scenario_repo
from scorekeeper.db.repositories import use_cases as use_case_repo

logger = logging.getLogger(__name__)

# A scenario nobody has scored yet — the ``ScenarioResult.status`` model default, and
# the only status that may still take another platform execution.
SCENARIO_PENDIENTE = "pending"


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
    reuse_scenarios: bool = False,
    run_label: str | None = None,
    session: AsyncSession | None = None,
) -> str:
    """Parse ``files`` and persist a queued run for ``platform``; return its ``run_id``.

    Builds one ``BenchmarkRun`` and, under it, one ``ScenarioResult`` per distinct
    ``scenario_id`` — so **files sharing a scenario id are grouped**, which is how one
    upload compares the same task across platforms. Each file becomes one
    ``PlatformExecution`` (with its ``Turn`` rows) under its scenario, carrying
    ``upload.platform`` when set and the run-level ``platform`` fallback otherwise.

    With ``reuse_scenarios`` (the capture path; ``POST /evaluations`` leaves it off) a
    scenario id that already names a still-``pending`` scenario of an *unstarted* run —
    one still at ``ingerido`` — is extended rather than recreated: the executions are
    appended to that scenario and the existing run is the one returned. A run that has
    already been started or scored is never touched, and neither is a scenario that has
    rolled up past ``pending``, because unscored turns would corrupt their roll-ups; both
    cases ingest a new run exactly as before.

    ``run_label`` (also capture-only, and ignored without ``reuse_scenarios``) groups one
    tier higher: the run is resolved as the newest still-``ingerido`` run carrying that
    label, or opened carrying it when there is none, and the scenario reuse above is then
    **scoped to that run**. So captures naming *different* scenarios but the same label
    land under one run — the batch the client named — while an unlabelled capture keeps
    matching a pending scenario anywhere.

    The run is committed with status ``ingerido`` (persisted, not yet started) and left
    unscored; a caller later starts it via :func:`start_run` (which enqueues the
    per-turn evaluation chain).

    This is the fast, on-request half: parsing is synchronous so a malformed sheet is
    rejected here (as ``ValueError``) before anything is persisted. ``session`` defaults
    to ``SessionLocal()``; tests inject an in-memory session.

    Raises ``ValueError`` when ``platform`` or ``files`` is empty, when a file cannot be
    parsed (propagated from ``parse_conversation``), or when two files share a
    ``scenario_id`` but name different use cases — or a reused scenario names another one — they would land in one scenario and
    only one metric set can score it, so the ambiguity is a client error rather than a
    silent pick. The transaction is rolled back first so nothing is persisted.
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

            # Resolved before the run, because a reused scenario brings its own: the new
            # executions belong to the run that already holds the scenario.
            scenarios: dict[str, ScenarioResult] = {}
            run: BenchmarkRun | None = None
            if reuse_scenarios:
                if run_label:
                    # The batch the client named decides the run; a label pointing at a
                    # run that already started is free again, and opens a new one below.
                    run = await run_repo.latest_run_for_label(
                        db, run_label, run_status=STATUS_INGERIDO
                    )
                # A label with no run yet starts an empty batch: there is nothing of that
                # run to extend, and a scenario of another run is not part of the batch.
                if run is not None or not run_label:
                    for scenario_id in OrderedDict.fromkeys(u.scenario_id for u, _, _ in parsed):
                        existing = await scenario_repo.latest_scenario_for_reuse(
                            db,
                            scenario_id,
                            run_status=STATUS_INGERIDO,
                            # The model default, i.e. "nothing has scored this scenario yet".
                            # A scenario that has rolled up to any other status keeps the
                            # executions it was scored on: appending one now would leave its
                            # roll-up describing a set of executions that no longer exists.
                            scenario_status=SCENARIO_PENDIENTE,
                            run_key=run.id if run is not None else None,
                        )
                        if existing is not None:
                            scenarios[scenario_id] = existing
                            run = run or existing.run
            reused = set(scenarios)

            if run is None:
                run = BenchmarkRun(status=STATUS_INGERIDO, label=run_label or None)
                # A run references a single source file; only meaningful with one upload.
                if len(parsed) == 1:
                    run.source_file = parsed[0][2]

            # One ScenarioResult per distinct scenario_id (first-seen order), one
            # PlatformExecution per file under it: the platform is a property of the
            # captured conversation, and each file's optional override wins over the
            # run-level fallback.
            for upload, messages, _ in parsed:
                scenario = scenarios.get(upload.scenario_id)
                if scenario is None:
                    scenario = ScenarioResult(
                        scenario_id=upload.scenario_id,
                        use_case_id=use_case_ids[upload.use_case],
                        run=run,
                    )
                    # Added explicitly for the same reason the execution below is: a
                    # scenario hung off an *already persistent* run (the labelled batch)
                    # is not cascaded into the session by the backref.
                    scenarios[upload.scenario_id] = scenario
                elif scenario.use_case_id != use_case_ids[upload.use_case]:
                    conflict = (
                        "ya está ingerido con otro caso de uso"
                        if upload.scenario_id in reused
                        else "llega con dos casos de uso distintos"
                    )
                    raise ValueError(
                        f"El escenario {upload.scenario_id!r} {conflict}; solo un "
                        f"conjunto de métricas puede puntuarlo."
                    )
                platform_exec = PlatformExecution(
                    platform=upload.platform or platform,
                    model_name=upload.model_name,
                    source_ref=upload.filename,
                    raw_conversation={"messages": messages},
                    scenario_result=scenario,
                )
                # Added explicitly: since 2.0 SQLAlchemy no longer cascades save-update
                # along a backref, so an execution hung off an *already persistent*
                # scenario (the reuse path) would never be flushed.
                db.add(platform_exec)
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
                    platform_exec.turns.append(turn_row)

            db.add(run)
            # Counted from the parsed input rather than by walking the committed run tree:
            # the relationships were built in memory here, but re-reading them after the
            # commit would be a lazy load the async session cannot serve.
            turn_total = sum(len(project_turns(messages)) for _, messages, _ in parsed)
            await db.commit()
            logger.info(
                "Run %s (lote=%s) ingerido: %d escenario(s), %d archivo(s) = %d turno(s).",
                run.id,
                run.label,
                len(scenarios),
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
