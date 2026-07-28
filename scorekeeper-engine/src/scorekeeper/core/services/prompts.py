"""Read the prompt catalog — the slots each metric renders and their version history.

Read-only for now. Editing (draft → publish → activate/rollback) and binding a run to the
versions it scored under are the next slice; the tables already carry both, so nothing
here has to change to accommodate them.

There is no delete, and there never will be: a version a finished benchmark points at is
what makes that benchmark's scores readable. An unwanted draft is discarded and an
unwanted published version deactivated.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.core.metrics.selection import sync_metrics, sync_prompts
from scorekeeper.db.connection import session_scope
from scorekeeper.db.models import Prompt, PromptVersion
from scorekeeper.db.repositories import prompts as repo


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
