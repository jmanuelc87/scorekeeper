"""Queries over ``prompts`` and ``prompt_versions``."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.db.models import (
    MetricDefinition,
    Prompt,
    PromptVersion,
    RunPromptBinding,
)


async def slots_by_key(session: AsyncSession) -> dict[tuple[uuid.UUID, str], Prompt]:
    """Every prompt slot keyed by ``(metric_id, slug)`` — what ``sync_prompts`` reconciles.

    Whole rows rather than keys, because the sync also refreshes the code-owned contract
    columns on a slot that already exists.
    """
    rows = (await session.execute(select(Prompt))).scalars()
    return {(row.metric_id, row.slug): row for row in rows}


async def active_versions_for(
    session: AsyncSession, metric_names: list[str]
) -> dict[tuple[str, str], PromptVersion]:
    """The active published version of every slot belonging to ``metric_names``.

    Keyed by ``(metric name, slug)``. One query for the whole run's metric set — the
    scoring runner resolves once and pins the result, so this must not be per metric.
    """
    if not metric_names:
        return {}
    stmt = (
        select(MetricDefinition.name, Prompt.slug, PromptVersion)
        .join(Prompt, Prompt.metric_id == MetricDefinition.id)
        .join(PromptVersion, PromptVersion.prompt_id == Prompt.id)
        .where(MetricDefinition.name.in_(metric_names), PromptVersion.is_active)
    )
    return {(name, slug): version for name, slug, version in await session.execute(stmt)}


async def replace_run_bindings(
    session: AsyncSession, run_id: uuid.UUID, version_ids: Iterable[uuid.UUID]
) -> None:
    """Make ``run_id``'s bindings exactly ``version_ids``.

    Delete-then-insert rather than insert, so re-scoring a run is idempotent against
    ``uq_run_prompt_binding`` and the binding set always means "the versions the latest
    scoring used" — the same replacement semantics the run's ``MetricScore`` rows have.
    """
    await session.execute(
        delete(RunPromptBinding).where(RunPromptBinding.run_id == run_id)
    )
    for version_id in version_ids:
        session.add(RunPromptBinding(run_id=run_id, prompt_version_id=version_id))


async def list_with_active(
    session: AsyncSession,
) -> list[tuple[Prompt, str, PromptVersion | None]]:
    """Every prompt slot with its metric name and its active version.

    One outer-joined query rather than a query per slot, and an outer join on the version
    so a slot whose only version was deactivated still appears (with ``None``).
    """
    stmt = (
        select(Prompt, MetricDefinition.name, PromptVersion)
        .join(MetricDefinition, MetricDefinition.id == Prompt.metric_id)
        .outerjoin(
            PromptVersion,
            (PromptVersion.prompt_id == Prompt.id) & PromptVersion.is_active,
        )
        .order_by(MetricDefinition.name, Prompt.slug)
    )
    return [tuple(row) for row in await session.execute(stmt)]


async def get_with_versions(
    session: AsyncSession, prompt_id: uuid.UUID
) -> tuple[Prompt, str, list[PromptVersion]] | None:
    """One slot with its metric name and its full version history, newest first.

    ``None`` when the id is unknown — the caller turns that into a 404.
    """
    stmt = (
        select(Prompt, MetricDefinition.name)
        .join(MetricDefinition, MetricDefinition.id == Prompt.metric_id)
        .where(Prompt.id == prompt_id)
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return None

    prompt, metric_name = row
    versions = (
        await session.execute(
            select(PromptVersion)
            .where(PromptVersion.prompt_id == prompt_id)
            .order_by(PromptVersion.version.desc())
        )
    ).scalars()
    return prompt, metric_name, list(versions)
