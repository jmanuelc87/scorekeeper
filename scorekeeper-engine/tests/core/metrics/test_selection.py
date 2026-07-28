"""The seam between the code registry and the stored use-case sets.

``sync_metrics`` mirrors the registered metric *names* into the ``metrics`` table and
keeps the ``default`` use case scoring all of them. Every *other* use case is user data
(``POST /use-cases``), so these tests build those links directly and check that
``metrics_for``/``resolve`` read them back. Nothing here is ever deleted — a metric
dropped from the registry may still be referenced by a stored set and by historical
``metric_scores``.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.db.models import (
    MetricDefinition,
    Prompt,
    PromptVersion,
    UseCase,
    UseCaseMetric,
)
from scorekeeper.db.repositories import use_cases as repo
from scorekeeper.core.metrics.base import Metric, MetricResult, TurnView
from scorekeeper.core.metrics.category import MetricCategory
from scorekeeper.core.metrics.prompts import PromptSlot
from scorekeeper.core.metrics.registry import MetricRegistry
from scorekeeper.core.metrics.scale import Unit
from scorekeeper.core.metrics.selection import (
    DEFAULT_USE_CASE,
    MissingPromptError,
    active_templates,
    metrics_for,
    resolve,
    sync_metrics,
    sync_prompts,
)

CATALOG_NAMES = {
    "contextual_precision",
    "hallucination",
    "faithfulness_ragas",
    "faithfulness_deepeval",
}


class _Nueva(Metric):
    """A metric added to the registry after the first sync."""

    name: ClassVar[str] = "nueva"
    category = MetricCategory.RAG
    scale = Unit()

    def evaluate(self, turn: TurnView, judge) -> MetricResult:  # pragma: no cover - unused
        raise NotImplementedError


class _ConPrompt(Metric):
    """A metric declaring a prompt slot, added to the registry after the first sync."""

    name: ClassVar[str] = "con_prompt"
    category = MetricCategory.RAG
    scale = Unit()
    prompts = (PromptSlot(slug="saludo", required_variables=("nombre",)),)

    def evaluate(self, turn: TurnView, judge) -> MetricResult:  # pragma: no cover - unused
        raise NotImplementedError


async def _link(session: AsyncSession, use_case_name: str, metric_names: list[str]) -> UseCase:
    """Create a use case scoring ``metric_names``, reusing existing ``metrics`` rows."""
    use_case = UseCase(name=use_case_name)
    session.add(use_case)
    await session.flush()
    for metric_name in metric_names:
        metric = (
            await session.execute(
                select(MetricDefinition).where(MetricDefinition.name == metric_name)
            )
        ).scalars().one_or_none()
        if metric is None:
            metric = MetricDefinition(name=metric_name)
            session.add(metric)
            await session.flush()
        session.add(UseCaseMetric(use_case_id=use_case.id, metric_id=metric.id))
    await session.commit()
    return use_case


async def test_sync_materializes_the_registry_catalog(
    db_session: AsyncSession, registered_metrics
) -> None:
    await sync_metrics(db_session)
    await db_session.commit()

    names = set((await db_session.execute(select(MetricDefinition.name))).scalars())
    assert names == CATALOG_NAMES


async def test_sync_seeds_default_scoring_every_registered_metric(
    db_session: AsyncSession, registered_metrics
) -> None:
    await sync_metrics(db_session)
    await db_session.commit()

    default = (
        await db_session.execute(select(UseCase).where(UseCase.name == DEFAULT_USE_CASE))
    ).scalars().one()
    # An upload that names no use case still gets the full evaluation.
    assert set(await metrics_for(db_session, default.id)) == CATALOG_NAMES


async def test_sync_adds_a_new_metric_to_default(
    db_session: AsyncSession, registered_metrics
) -> None:
    await sync_metrics(db_session)
    await db_session.commit()

    MetricRegistry.add(_Nueva)
    await sync_metrics(db_session)
    await db_session.commit()

    # A metric added to the catalog joins "default" on the next sync — no migration.
    default_id = (await repo.use_case_ids(db_session))[DEFAULT_USE_CASE]
    assert set(await metrics_for(db_session, default_id)) == CATALOG_NAMES | {"nueva"}


async def test_sync_leaves_other_use_cases_alone(
    db_session: AsyncSession, registered_metrics
) -> None:
    use_case = await _link(db_session, "soporte", ["contextual_precision"])

    await sync_metrics(db_session)
    await db_session.commit()

    # Only "default" is reconciled; a set composed through the API owns its metrics.
    assert await metrics_for(db_session, use_case.id) == ["contextual_precision"]


async def test_sync_is_idempotent(db_session: AsyncSession, registered_metrics) -> None:
    await sync_metrics(db_session)
    await db_session.commit()
    count_1 = (
        await db_session.execute(select(func.count()).select_from(MetricDefinition))
    ).scalar_one()

    await sync_metrics(db_session)
    await db_session.commit()
    count_2 = (
        await db_session.execute(select(func.count()).select_from(MetricDefinition))
    ).scalar_one()

    assert count_1 == count_2


async def test_sync_keeps_metrics_the_registry_no_longer_declares(
    db_session: AsyncSession, registered_metrics
) -> None:
    db_session.add(MetricDefinition(name="metrica_retirada"))
    await db_session.commit()

    await sync_metrics(db_session)
    await db_session.commit()

    # Insert-only: a stored set and historical metric_scores may still reference it.
    names = set((await db_session.execute(select(MetricDefinition.name))).scalars())
    assert "metrica_retirada" in names


async def test_metrics_for_reads_the_linked_set(
    db_session: AsyncSession, registered_metrics
) -> None:
    await sync_metrics(db_session)
    use_case = await _link(
        db_session, "soporte", ["hallucination", "contextual_precision"]
    )

    assert await metrics_for(db_session, use_case.id) == [
        "contextual_precision",
        "hallucination",
    ]


async def test_resolve_returns_metric_instances(
    db_session: AsyncSession, registered_metrics
) -> None:
    await _sync_all(db_session)
    # Every declared slot needs a live version now — resolve binds templates, it does
    # not fall back to a constant in code.
    await _publish_all(db_session)
    use_case = await _link(db_session, "soporte", ["contextual_precision"])

    metrics = await resolve(db_session, use_case.id)
    assert all(isinstance(metric, Metric) for metric in metrics)
    assert {metric.name for metric in metrics} == {"contextual_precision"}


async def test_resolve_raises_on_unknown_stored_metric(
    db_session: AsyncSession, registered_metrics
) -> None:
    use_case = await _link(db_session, "raro", ["fantasma"])

    with pytest.raises(KeyError, match="Métrica desconocida"):
        await resolve(db_session, use_case.id)


# --- sync_prompts / active_templates ------------------------------------------
# The same two-halves split one level down, but the halves sit differently: a slot's
# slug, required variables and description are code, and ``sync_prompts`` reconciles
# them. The *text* is not code at all — it is seeded by the prompt-catalog migration,
# so ``sync_prompts`` never writes a version and a slot it creates has none.


async def _slots(session: AsyncSession) -> dict[str, list[str]]:
    """``metric name -> slugs`` currently materialized in ``prompts``."""
    stmt = select(MetricDefinition.name, Prompt.slug).join(
        Prompt, Prompt.metric_id == MetricDefinition.id
    )
    found: dict[str, list[str]] = {}
    for metric_name, slug in await session.execute(stmt):
        found.setdefault(metric_name, []).append(slug)
    return {name: sorted(slugs) for name, slugs in found.items()}


async def _sync_all(session: AsyncSession) -> None:
    await sync_metrics(session)
    await session.flush()
    await sync_prompts(session)
    await session.flush()


async def _publish_all(session: AsyncSession, template: str = "Plantilla {claim}") -> None:
    """Give every prompt row a published, active v1 — what the migration does."""
    for prompt in (await session.execute(select(Prompt))).scalars().all():
        session.add(
            PromptVersion(
                prompt_id=prompt.id,
                version=1,
                template=template,
                status="published",
                is_active=True,
            )
        )
    await session.flush()


async def test_sync_prompts_materializes_every_declared_slot(
    db_session: AsyncSession, registered_metrics
) -> None:
    await _sync_all(db_session)

    assert await _slots(db_session) == {
        "contextual_precision": ["verdict"],
        "faithfulness_deepeval": ["generate_truths", "verify"],
        "faithfulness_ragas": ["verify"],
        "hallucination": ["nli"],
    }


async def test_sync_prompts_records_the_code_owned_contract(
    db_session: AsyncSession, registered_metrics
) -> None:
    await _sync_all(db_session)

    row = (
        await db_session.execute(
            select(Prompt)
            .join(MetricDefinition, MetricDefinition.id == Prompt.metric_id)
            .where(MetricDefinition.name == "hallucination")
        )
    ).scalars().one()

    assert row.required_variables == ["documento", "response"]
    assert "NLI" in (row.description or "")


async def test_sync_prompts_writes_no_version(
    db_session: AsyncSession, registered_metrics
) -> None:
    """The text is the migration's to seed, never code's to invent."""
    await _sync_all(db_session)

    count = (await db_session.execute(select(func.count(PromptVersion.id)))).scalar_one()
    assert count == 0


async def test_sync_prompts_is_idempotent(
    db_session: AsyncSession, registered_metrics
) -> None:
    await _sync_all(db_session)
    await _sync_all(db_session)

    count = (await db_session.execute(select(func.count(Prompt.id)))).scalar_one()
    assert count == 5  # the fixture registry omits AnswerRelevance; production has 6


async def test_sync_prompts_refreshes_a_changed_contract(
    db_session: AsyncSession, registered_metrics
) -> None:
    """required_variables and description track the code; a stale row lies to the editor."""
    await _sync_all(db_session)
    row = (
        await db_session.execute(
            select(Prompt)
            .join(MetricDefinition, MetricDefinition.id == Prompt.metric_id)
            .where(MetricDefinition.name == "faithfulness_ragas")
        )
    ).scalars().one()
    row.required_variables = ["obsoleta"]
    row.description = "vieja"
    await db_session.flush()

    await _sync_all(db_session)

    await db_session.refresh(row)
    assert row.required_variables == ["claim"]
    assert row.description != "vieja"


async def test_sync_prompts_never_touches_stored_text(
    db_session: AsyncSession, registered_metrics
) -> None:
    """A resync must not undo an edit — the version is data, not a projection of code."""
    await _sync_all(db_session)
    prompt = (await db_session.execute(select(Prompt))).scalars().first()
    db_session.add(
        PromptVersion(
            prompt_id=prompt.id,
            version=1,
            template="Rúbrica editada {claim}",
            status="published",
            is_active=True,
        )
    )
    await db_session.flush()

    await _sync_all(db_session)

    stored = (await db_session.execute(select(PromptVersion.template))).scalars().all()
    assert stored == ["Rúbrica editada {claim}"]


async def test_sync_prompts_picks_up_a_slot_added_later(
    db_session: AsyncSession, registered_metrics
) -> None:
    """A metric added to the registry brings its slots on the next sync, with no migration."""
    await _sync_all(db_session)
    MetricRegistry.add(_ConPrompt)

    await _sync_all(db_session)

    assert (await _slots(db_session))["con_prompt"] == ["saludo"]


async def test_active_templates_returns_the_active_version(
    db_session: AsyncSession, registered_metrics
) -> None:
    await _sync_all(db_session)
    prompt = (
        await db_session.execute(
            select(Prompt)
            .join(MetricDefinition, MetricDefinition.id == Prompt.metric_id)
            .where(MetricDefinition.name == "faithfulness_ragas")
        )
    ).scalars().one()
    db_session.add(
        PromptVersion(
            prompt_id=prompt.id,
            version=7,
            template="Afirmación: {claim}",
            status="published",
            is_active=True,
        )
    )
    await db_session.flush()

    resolved = await active_templates(db_session, ["faithfulness_ragas"])

    assert resolved["faithfulness_ragas"]["verify"].template == "Afirmación: {claim}"
    assert resolved["faithfulness_ragas"]["verify"].version == 7


async def test_active_templates_ignores_inactive_versions(
    db_session: AsyncSession, registered_metrics
) -> None:
    """A superseded version is history, not something a run can bind."""
    await _sync_all(db_session)
    prompt = (
        await db_session.execute(
            select(Prompt)
            .join(MetricDefinition, MetricDefinition.id == Prompt.metric_id)
            .where(MetricDefinition.name == "faithfulness_ragas")
        )
    ).scalars().one()
    db_session.add(
        PromptVersion(
            prompt_id=prompt.id, version=1, template="vieja {claim}", status="published"
        )
    )
    await db_session.flush()

    with pytest.raises(MissingPromptError, match="faithfulness_ragas.verify"):
        await active_templates(db_session, ["faithfulness_ragas"])


async def test_active_templates_lists_every_missing_slot_at_once(
    db_session: AsyncSession, registered_metrics
) -> None:
    """One message naming all of them beats dying on the first."""
    await _sync_all(db_session)

    with pytest.raises(MissingPromptError) as exc:
        await active_templates(db_session, ["faithfulness_deepeval"])

    assert "faithfulness_deepeval.generate_truths" in str(exc.value)
    assert "faithfulness_deepeval.verify" in str(exc.value)


async def test_active_templates_of_nothing_is_empty(
    db_session: AsyncSession, registered_metrics
) -> None:
    assert await active_templates(db_session, []) == {}


async def test_resolve_injects_the_stored_template(
    db_session: AsyncSession, registered_metrics
) -> None:
    """The payoff: editing the stored text changes what the metric renders."""
    await _sync_all(db_session)
    use_case = await _link(db_session, "soporte", ["faithfulness_ragas"])
    await _publish_all(db_session, "Editada: {claim}")

    metrics = await resolve(db_session, use_case.id)

    assert metrics[0].prompt("verify") == "Editada: {claim}"
