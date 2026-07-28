"""Service-level tests for the prompt catalog's write paths.

Only the two behaviours HTTP cannot reach cleanly: that publishing reconciles the
code-owned contract before validating against it, and that a rejected publish leaves
the draft untouched. Everything else is covered end-to-end in
``tests/api/v1/test_prompts_endpoint.py``.
"""

from __future__ import annotations

import uuid
from typing import ClassVar

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.core.metrics.base import Metric, MetricResult, TurnView
from scorekeeper.core.metrics.category import MetricCategory
from scorekeeper.core.metrics.prompts import PromptSlot
from scorekeeper.core.metrics.registry import MetricRegistry
from scorekeeper.core.metrics.scale import Unit
from scorekeeper.core.services import prompts as service
from scorekeeper.db.models import Prompt, PromptVersion


class Utilidad(Metric):
    """A one-slot metric whose contract requires two variables."""

    name: ClassVar[str] = "utilidad"
    category = MetricCategory.RAG
    scale = Unit()
    prompts = (
        PromptSlot(
            slug="verify",
            required_variables=("truths", "claim"),
            description="Veredicto por afirmación.",
        ),
    )

    def evaluate(self, turn: TurnView, judge) -> MetricResult:  # pragma: no cover - unused
        raise NotImplementedError


@pytest.fixture
def registry():
    """Isolate the registry to the fake metric above, then restore."""
    saved = MetricRegistry.all()
    MetricRegistry.clear()
    MetricRegistry.add(Utilidad)
    try:
        yield
    finally:
        MetricRegistry.clear()
        for metric_cls in saved:
            MetricRegistry.add(metric_cls)


async def _slot(session: AsyncSession) -> Prompt:
    """Materialize the catalog and return the ``verify`` slot."""
    await service.list_prompts(session=session)
    return (
        await session.execute(select(Prompt).where(Prompt.slug == "verify"))
    ).scalars().one()


async def test_publish_reconciles_the_contract_before_validating(
    session: AsyncSession, registry
) -> None:
    """A slot whose declared contract widened in a deploy must accept the new variable.

    ``required_variables`` is code-owned and mirrored into the row by ``sync_prompts``.
    Without the sync at the top of ``publish_version``, a database that has not been
    ingested against since the deploy still holds the narrow contract, and a correct
    template using the newly declared variable would be rejected as "unknown" — a wrong
    answer, not merely a stale read.
    """
    slot = await _slot(session)
    # Simulate the stale row: the metric above declares ("truths", "claim").
    slot.required_variables = ["claim"]
    await session.commit()

    draft = await service.create_version(
        str(slot.id), "{truths} y {claim}", session=session
    )
    published = await service.publish_version(
        str(slot.id), draft["id"], session=session
    )

    assert published["status"] == "published"
    assert published["is_active"] is True


async def test_publish_does_not_change_the_draft_when_validation_fails(
    session: AsyncSession, registry
) -> None:
    """A rejected publish is a no-op: the draft stays open and nothing goes live."""
    slot = await _slot(session)
    draft = await service.create_version(str(slot.id), "Solo {claim}.", session=session)

    with pytest.raises(service.PromptValidationError):
        await service.publish_version(str(slot.id), draft["id"], session=session)

    row = await session.get(PromptVersion, uuid.UUID(draft["id"]))
    await session.refresh(row)
    assert row.status == "draft"
    assert row.is_active is False
    assert row.published_at is None
