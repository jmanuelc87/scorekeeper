"""The prompt catalog — the slots each metric renders, their text, and its history.

Reads list the slots and their versions; writes move one slot through its lifecycle:
an edit lands as a ``draft``, publishing validates it and makes it live, and rollback
activates an earlier published version rather than copying its text forward.

``template`` is write-once, including while a version is still a draft: saving over an
open draft discards that row and inserts a new one, so the per-prompt version counter
has gaps by design and no text a reviewer has already seen can change under them.

Two rules the routes rely on:

* **Publishing activates.** A published-but-inactive version is exactly the state
  ``active_templates`` refuses to score under, so leaving a slot there would be a
  foot-gun with no caller.
* **There is no deactivate, and no delete.** ``is_active`` is only ever cleared as half
  of installing a replacement, so a slot that has a live version always has one; and a
  version a finished benchmark points at is what makes that benchmark's scores
  readable. An unwanted draft is discarded, an unwanted published version superseded.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.core.metrics.prompts import PromptTemplateError, validate_template
from scorekeeper.core.metrics.selection import sync_metrics, sync_prompts
from scorekeeper.db.connection import session_scope
from scorekeeper.db.models import Prompt, PromptVersion
from scorekeeper.db.repositories import prompts as repo


class PromptError(Exception):
    """Base class for prompt-catalog service errors."""


class PromptValidationError(PromptError):
    """A blank template, or one that does not satisfy its slot's contract. → HTTP 422."""


class PromptConflictError(PromptError):
    """A transition the version's current status does not allow. → HTTP 409."""


def _serialize_version(version: PromptVersion | None) -> dict[str, Any] | None:
    if version is None:
        return None
    return {
        "id": str(version.id),
        "version": version.version,
        "template": version.template,
        "status": version.status,
        "is_active": version.is_active,
        "changelog": version.changelog,
        "created_by": version.created_by,
        "created_at": version.created_at,
        "published_by": version.published_by,
        "published_at": version.published_at,
    }


def _serialize(prompt: Prompt, metric_name: str) -> dict[str, Any]:
    return {
        "id": str(prompt.id),
        "metric": metric_name,
        "slug": prompt.slug,
        "required_variables": list(prompt.required_variables or []),
        "description": prompt.description or "",
    }


async def list_prompts(*, session: AsyncSession | None = None) -> list[dict[str, Any]]:
    """Every prompt slot with its active version, ordered by metric then slug.

    Materializes the catalog before reading, the same insert-only reconciliation
    ``create_use_case`` performs: a metric whose slots have never been synced (a fresh
    database, or a slot added since the last ingest) would otherwise be invisible here
    until somebody happened to upload a file.
    """
    async with session_scope(session) as db:
        await sync_metrics(db)
        await db.flush()
        await sync_prompts(db)
        await db.commit()

        return [
            {**_serialize(prompt, metric_name), "active_version": _serialize_version(active)}
            for prompt, metric_name, active in await repo.list_with_active(db)
        ]


async def get_prompt(
    prompt_id: str, *, session: AsyncSession | None = None
) -> dict[str, Any] | None:
    """One slot with its full version history, newest first; ``None`` if unknown.

    A malformed id is "unknown" rather than an error — the router turns both into a 404.
    """
    try:
        key = uuid.UUID(prompt_id)
    except ValueError:
        return None

    async with session_scope(session) as db:
        found = await repo.get_with_versions(db, key)
        if found is None:
            return None
        prompt, metric_name, versions = found
        return {
            **_serialize(prompt, metric_name),
            "versions": [_serialize_version(version) for version in versions],
        }


def _key(value: str) -> uuid.UUID | None:
    """Parse a path id; ``None`` when malformed — the router turns that into a 404."""
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def _load(
    db: AsyncSession, prompt_id: str, version_id: str
) -> tuple[Prompt, PromptVersion, PromptVersion | None] | None:
    """The slot, the target version, and the slot's currently active version.

    ``None`` when either id is malformed or unknown — including a version that belongs
    to a *different* prompt, which is unknown by construction because the version is
    looked up within this slot's own history rather than globally.
    """
    prompt_key, version_key = _key(prompt_id), _key(version_id)
    if prompt_key is None or version_key is None:
        return None

    found = await repo.get_with_versions(db, prompt_key)
    if found is None:
        return None
    prompt, _, versions = found

    target = next((row for row in versions if row.id == version_key), None)
    if target is None:
        return None
    active = next((row for row in versions if row.is_active), None)
    return prompt, target, active


async def _activate(
    db: AsyncSession, version: PromptVersion, active: PromptVersion | None
) -> None:
    """Make ``version`` the slot's live one, clearing the incumbent first.

    The intermediate flush is load-bearing, not cosmetic. ``uq_prompt_version_active``
    is an immediate partial unique index on both dialects, and these are two UPDATEs on
    one table, which SQLAlchemy's unit of work emits ordered by primary key — random
    UUIDs. Without the flush the activate can be issued before the deactivate and
    violate the index, non-deterministically, depending on how the ids happened to sort.
    """
    if active is version:
        return  # already live; the rollback control is idempotent.
    if active is not None:
        active.is_active = False
        await db.flush()
    version.is_active = True


async def create_version(
    prompt_id: str,
    template: str,
    *,
    changelog: str | None = None,
    author: str | None = None,
    session: AsyncSession | None = None,
) -> dict[str, Any] | None:
    """Open a new draft for ``prompt_id``; ``None`` when the id is unknown or malformed.

    Saving over an open draft discards that row and creates a new one with the next
    version number: ``template`` is write-once even as a draft, so the counter has gaps
    by design. ``supersedes_id`` points at the active published version — the live text
    this edit moves away from — or is NULL when the slot has never had one, which is
    also why a draft replacing a draft points at the same published parent rather than
    at the row it displaced.

    The template is deliberately *not* validated here; that gate is ``publish_version``.
    Only a blank one is refused, with ``PromptValidationError``.
    """
    if not template.strip():
        raise PromptValidationError("La plantilla no puede estar vacía.")

    key = _key(prompt_id)
    if key is None:
        return None

    async with session_scope(session) as db:
        found = await repo.get_with_versions(db, key)
        if found is None:
            return None
        _, _, versions = found

        # Discard before inserting, and flush between: both rows contend for
        # ``uq_prompt_version_draft``, and the ORM's update-before-insert ordering is an
        # implementation detail whose failure mode here would be an IntegrityError.
        for row in versions:
            if row.status == "draft":
                row.status = "discarded"
        await db.flush()

        active = next((row for row in versions if row.is_active), None)
        # Stored unstripped: whitespace in a multi-line prompt is the author's, and the
        # row is a permanent record of what a benchmark scored under.
        draft = PromptVersion(
            prompt_id=key,
            version=max((row.version for row in versions), default=0) + 1,
            template=template,
            status="draft",
            is_active=False,
            supersedes_id=active.id if active is not None else None,
            changelog=changelog,
            created_by=author,
        )
        db.add(draft)
        await db.commit()
        return _serialize_version(draft)


async def publish_version(
    prompt_id: str,
    version_id: str,
    *,
    author: str | None = None,
    session: AsyncSession | None = None,
) -> dict[str, Any] | None:
    """Validate a draft against its slot's contract and make it the live version.

    The one place ``validate_template`` runs in production. Publishing activates, and
    deactivates the incumbent in the same transaction.

    Raises ``PromptValidationError`` (422) when the template does not satisfy the slot
    and ``PromptConflictError`` (409) when the version is not a draft; returns ``None``
    (404) for an unknown prompt or version.
    """
    async with session_scope(session) as db:
        # The only write path that reads the code-owned ``required_variables``. A slot
        # whose contract widened in a deploy nobody has ingested against since would
        # otherwise reject a correct template — a wrong answer, not just a stale read.
        await sync_prompts(db)
        await db.flush()

        loaded = await _load(db, prompt_id, version_id)
        if loaded is None:
            return None
        prompt, version, active = loaded

        if version.status != "draft":
            raise PromptConflictError(
                f"La versión {version.version} no es un borrador; "
                "solo se puede publicar un borrador."
            )

        try:
            validate_template(version.template, list(prompt.required_variables or []))
        except PromptTemplateError as exc:
            # Subclass of ValueError, so this clause must come first.
            raise PromptValidationError(str(exc)) from exc
        except ValueError as exc:
            # ``string.Formatter`` on unbalanced braces, with an English message.
            raise PromptValidationError(
                "La plantilla tiene llaves sin cerrar o mal balanceadas."
            ) from exc

        # Status before the commit flushes: ``ck_prompt_version_active_published``
        # rejects an active draft, and ``_activate`` leaves this row active.
        await _activate(db, version, active)
        version.status = "published"
        version.published_by = author
        version.published_at = datetime.now(timezone.utc)
        await db.commit()
        return _serialize_version(version)


async def activate_version(
    prompt_id: str, version_id: str, *, session: AsyncSession | None = None
) -> dict[str, Any] | None:
    """Roll the live version back to an already-published one.

    A no-op returning the version unchanged when it is already active, so the rollback
    control is idempotent. Raises ``PromptConflictError`` (409) for a draft or a
    discarded version — activating either is also what ``ck_prompt_version_active_published``
    forbids. Never leaves the slot dark: the incumbent is only cleared as part of
    installing its replacement.
    """
    async with session_scope(session) as db:
        loaded = await _load(db, prompt_id, version_id)
        if loaded is None:
            return None
        _, version, active = loaded

        if version.status != "published":
            raise PromptConflictError(
                f"La versión {version.version} no está publicada; "
                "solo se puede activar una versión publicada."
            )

        await _activate(db, version, active)
        await db.commit()
        return _serialize_version(version)


async def discard_version(
    prompt_id: str, version_id: str, *, session: AsyncSession | None = None
) -> dict[str, Any] | None:
    """Abandon an open draft.

    Drafts only. A published version cannot be discarded even when it is inactive: a
    finished run's ``RunPromptBinding`` points at it, and "discarded" would then claim
    that run scored under nothing. Supersede it by activating another instead.
    """
    async with session_scope(session) as db:
        loaded = await _load(db, prompt_id, version_id)
        if loaded is None:
            return None
        _, version, _ = loaded

        if version.status != "draft":
            raise PromptConflictError(
                f"La versión {version.version} no es un borrador; "
                "solo se puede descartar un borrador."
            )

        version.status = "discarded"
        await db.commit()
        return _serialize_version(version)
