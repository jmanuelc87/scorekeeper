"""The seed↔slot contract: the migration's prompt text against the code's declarations.

Now that the templates live only in the migration, nothing at import time relates them
to the metric classes that render them. This module is that relation. It is the check
that catches a metric declaring a slot the migration never seeded (scoring would refuse
to start), a seed for a slot no metric declares (dead text), and a template whose
placeholders do not match what the metric actually fills (a prompt reaching the judge
with a literal ``{claim}`` in it).

Deliberately validated against the **code** slot's ``required_variables``, not the
seed's own copy — validating the migration against itself would prove nothing. The two
copies are compared separately.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.core.metrics import catalog  # noqa: F401  (populate the registry)
from scorekeeper.core.metrics.prompts import (
    PromptTemplateError,
    placeholders,
    validate_template,
)
from scorekeeper.core.metrics.registry import MetricRegistry
from scorekeeper.db.models import MetricDefinition, Prompt, PromptVersion
from seeded_prompts import MODULES, SEEDED_PROMPTS, seed_prompts


_QUALITY_METRICS = (
    "relevancia",
    "precision",
    "completitud",
    "claridad",
    "razonamiento_logico",
    "contextualizacion",
    "accionabilidad",
    "estructura",
    "profundidad_analitica",
    "coherencia_multiturno",
)


def _declared_slots():
    return {
        (metric_cls.name, slot.slug): slot
        for metric_cls in MetricRegistry.all()
        for slot in metric_cls.prompts
    }


def _seeds_by_key():
    return {(seed["metric"], seed["slug"]): seed for seed in SEEDED_PROMPTS}


def test_every_declared_slot_is_seeded() -> None:
    """A slot with no seed means scoring that metric fails at run start."""
    missing = sorted(set(_declared_slots()) - set(_seeds_by_key()))
    assert not missing, f"Slots declarados sin semilla en la migración: {missing}"


def test_no_seed_is_orphaned() -> None:
    """A seed for a slot no metric declares is text nothing will ever render."""
    orphans = sorted(set(_seeds_by_key()) - set(_declared_slots()))
    assert not orphans, f"Semillas sin slot declarado: {orphans}"


def test_seeds_are_unique_per_slot() -> None:
    keys = [(seed["metric"], seed["slug"]) for seed in SEEDED_PROMPTS]
    assert len(keys) == len(set(keys))


@pytest.mark.parametrize("seed", SEEDED_PROMPTS, ids=lambda s: f"{s['metric']}.{s['slug']}")
def test_seeded_template_satisfies_its_slot(seed) -> None:
    """The template must use exactly what the metric fills, plus the judge's variables."""
    slot = _declared_slots()[(seed["metric"], seed["slug"])]
    try:
        validate_template(seed["template"], slot.required_variables)
    except PromptTemplateError as exc:  # pragma: no cover - only on a real bug
        pytest.fail(f"{seed['metric']}.{seed['slug']}: {exc}")


@pytest.mark.parametrize("seed", SEEDED_PROMPTS, ids=lambda s: f"{s['metric']}.{s['slug']}")
def test_seeded_required_variables_match_the_code(seed) -> None:
    """The migration's copy of the contract must not drift from the class's."""
    slot = _declared_slots()[(seed["metric"], seed["slug"])]
    assert tuple(seed["required_variables"]) == slot.required_variables


@pytest.mark.parametrize("seed", SEEDED_PROMPTS, ids=lambda s: f"{s['metric']}.{s['slug']}")
def test_seeded_template_is_not_blank(seed) -> None:
    assert seed["template"].strip()


# --- the seed actually executing ----------------------------------------------
# The suite never runs Alembic, so without this the migration's INSERTs are only
# ever read, never executed. This runs the real ``_seed_prompts`` against the test
# database and asserts the rows land exactly as the resolver expects to find them.


async def test_seed_prompts_inserts_a_published_active_v1(session: AsyncSession) -> None:
    for name in sorted({seed["metric"] for seed in SEEDED_PROMPTS}):
        session.add(MetricDefinition(name=name))
    await session.commit()

    await session.run_sync(lambda sync_session: seed_prompts(sync_session.connection()))
    await session.commit()

    rows = (
        await session.execute(
            select(MetricDefinition.name, Prompt.slug, Prompt.required_variables, PromptVersion)
            .join(Prompt, Prompt.metric_id == MetricDefinition.id)
            .join(PromptVersion, PromptVersion.prompt_id == Prompt.id)
        )
    ).all()

    assert len(rows) == len(SEEDED_PROMPTS)
    by_slot = {(name, slug): (required, version) for name, slug, required, version in rows}
    for seed in SEEDED_PROMPTS:
        required, version = by_slot[(seed["metric"], seed["slug"])]
        assert required == seed["required_variables"]
        assert version.template == seed["template"]
        assert (version.version, version.status, version.is_active) == (1, "published", True)
        assert version.created_by == version.published_by == "system"
        assert version.published_at is not None
        assert version.supersedes_id is None


async def test_seeded_rows_satisfy_the_active_version_index(session: AsyncSession) -> None:
    """One active published version per prompt — the partial unique index must accept it."""
    for name in sorted({seed["metric"] for seed in SEEDED_PROMPTS}):
        session.add(MetricDefinition(name=name))
    await session.commit()

    await session.run_sync(lambda sync_session: seed_prompts(sync_session.connection()))
    await session.commit()  # would raise IntegrityError if the seed violated it

    active = (
        await session.execute(
            select(func.count()).select_from(PromptVersion).where(PromptVersion.is_active)
        )
    ).scalar_one()
    assert active == len(SEEDED_PROMPTS)


async def test_seed_rejects_a_metric_with_no_row(session: AsyncSession) -> None:
    """Registry drift must fail loudly, not leave a prompt the runner will demand."""
    with pytest.raises(RuntimeError, match="No existe fila"):
        await session.run_sync(lambda s: seed_prompts(s.connection()))


# --- the rubric seed's own conditions ------------------------------------------
# d7f2b6c1a840 writes into tables that already hold rows, so unlike the catalog seed
# it must create the metric rows it needs and skip a slot that is already stored.

_RUBRICS = MODULES[-1]


async def test_rubric_seed_creates_its_own_metric_rows(session: AsyncSession) -> None:
    """No app has run ``sync_metrics`` at migrate time, so the FK targets must be inserted."""
    await session.run_sync(lambda s: _RUBRICS._seed_prompts(s.connection()))
    await session.commit()

    names = {seed["metric"] for seed in _RUBRICS.SEEDED_PROMPTS}
    stored = set((await session.execute(select(MetricDefinition.name))).scalars())
    assert names <= stored


async def test_rubric_seed_is_idempotent(session: AsyncSession) -> None:
    """A database whose app already booted has the metric rows; re-seeding must not duplicate."""
    for name in sorted({seed["metric"] for seed in _RUBRICS.SEEDED_PROMPTS}):
        session.add(MetricDefinition(name=name))
    await session.commit()

    for _ in range(2):
        await session.run_sync(lambda s: _RUBRICS._seed_prompts(s.connection()))
        await session.commit()

    prompts = (await session.execute(select(func.count()).select_from(Prompt))).scalar_one()
    assert prompts == len(_RUBRICS.SEEDED_PROMPTS)


# --- which slots are handed the retrieved context ------------------------------
# ``judges.base.render_prompt`` no longer appends the retrieved context to every call:
# it reaches the judge only through a template's own ``{context}``. So which prompts see
# it is now a property of the seed text, and this is the check on that.

_SLOTS_WITH_CONTEXT = {
    ("faithfulness_ragas", "verify"),
    ("faithfulness_deepeval", "generate_truths"),
    *((name, "rubric") for name in _QUALITY_METRICS),
}


@pytest.mark.parametrize("seed", SEEDED_PROMPTS, ids=lambda s: f"{s['metric']}.{s['slug']}")
def test_only_the_slots_that_need_context_interpolate_it(seed) -> None:
    key = (seed["metric"], seed["slug"])
    uses_context = "context" in placeholders(seed["template"])

    if key in _SLOTS_WITH_CONTEXT:
        assert uses_context, f"{key} necesita el contexto recuperado y ya no lo recibe."
    else:
        # An isolated judgement: the node, the document or the answer alone is the
        # premise, so handing it the whole context would change what it measures.
        assert not uses_context, f"{key} no debe recibir el contexto recuperado completo."
