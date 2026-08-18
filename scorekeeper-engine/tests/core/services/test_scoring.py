"""Tests for ``core.services.scoring`` and the run lifecycle in ``core.services.runs``."""

from __future__ import annotations

import uuid
from io import BytesIO
from typing import ClassVar

import pytest
from openpyxl import Workbook
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.core.metrics.base import (
    Metric,
    MetricResult,
    MetricTrace,
    TraceEntry,
    TraceStep,
    TurnView,
)
from scorekeeper.core.metrics.category import MetricCategory
from scorekeeper.core.metrics.judge import JudgeVerdict
from scorekeeper.core.metrics.prompts import PromptSlot, safe_format
from scorekeeper.core.metrics.registry import MetricRegistry
from scorekeeper.core.metrics.scale import Unit
from scorekeeper.core.metrics.selection import MissingPromptError, sync_prompts
from scorekeeper.core.services.ingestion import UploadedFile, ingest_evaluation
from scorekeeper.core.services.runs import get_run_summary, set_turn_selection, start_run
from scorekeeper.core.services.scoring import score_run
from scorekeeper.db.models import (
    BenchmarkRun,
    MetricScore,
    Prompt,
    PromptVersion,
    RunPromptBinding,
    ScenarioResult,
    Turn,
)


class RecordingJudge:
    """A Judge stub returning a fixed verdict (mirrors tests/test_runner.py)."""

    def __init__(self, score_value: float = 0.8, model: str = "judge-test") -> None:
        self.score_value = score_value
        self.model = model

    def model_for(self, step=None) -> str:
        return self.model

    def score(self, *, rubric, turn, scale, rubric_version=None, step=None) -> JudgeVerdict:
        return JudgeVerdict(score=self.score_value, justification="razón", model=self.model)

    def structured(self, *, instruction, turn, schema, step=None):  # pragma: no cover - unused
        raise NotImplementedError

    def embed(self, *, texts):  # pragma: no cover - unused
        raise NotImplementedError


class _FakeMetric(Metric):
    category = MetricCategory.RAG
    scale = Unit()
    rubric: ClassVar[str] = "¿Es buena la respuesta?"

    def evaluate(self, turn: TurnView, judge) -> MetricResult:
        verdict = judge.score(rubric=self.rubric, turn=turn, scale=self.scale)
        return MetricResult(
            metric_name=self.name,
            raw_score=verdict.score,
            normalized_score=self.normalize(verdict.score),
            trace=MetricTrace(
                steps=[
                    TraceStep(
                        label="Puntuación",
                        entries=[
                            TraceEntry(
                                label=self.name,
                                value=verdict.score,
                                justification=verdict.justification,
                            )
                        ],
                    )
                ]
            ),
            judge_model=verdict.model,
            rubric_version=self.rubric_version,
        )


class Utilidad(_FakeMetric):
    name = "utilidad"


@pytest.fixture
async def registry(session: AsyncSession, compose_use_case):
    """Register a single fake metric and link it to the ``default`` use case.

    Metric selection is user data now: no metric declares a use case, so an upload
    ingested under ``"default"`` scores nothing until a set links the two.
    """
    saved = MetricRegistry.all()
    MetricRegistry.clear()
    MetricRegistry.add(Utilidad)
    await compose_use_case([Utilidad.name])
    try:
        yield
    finally:
        MetricRegistry.clear()
        for metric_cls in saved:
            MetricRegistry.add(metric_cls)


def _xlsx_bytes(header: list[str], rows: list[list[object]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _conversation_bytes() -> bytes:
    return _xlsx_bytes(
        ["turn", "role", "content"],
        [
            [1, "user", "hola"],
            [1, "model", "qué tal"],
            [2, "user", "adiós"],
            [2, "model", "hasta luego"],
        ],
    )


async def _all_turn_ids(session: AsyncSession, run_id: str) -> list[str]:
    turns = (await session.execute(select(Turn))).scalars().all()
    return [str(t.id) for t in turns]


async def test_score_run_end_to_end(session: AsyncSession, registry) -> None:
    files = [UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default")]
    run_id = await ingest_evaluation("claude", files, session=session)
    # Scoring is opt-in per turn — select both turns before scoring.
    await set_turn_selection(
        run_id, await _all_turn_ids(session, run_id), True, session=session
    )

    summary = await score_run(run_id, session=session, judge=RecordingJudge(0.8))

    assert summary["run_id"] == run_id
    assert summary["status"] == "completado"
    assert summary["platforms"][0]["average_score"] == pytest.approx(0.8)
    # Fully scored -> progress pinned to 1.0.
    assert summary["progress"] == {"done": 2, "total": 2, "ratio": 1.0}
    scores = (await session.execute(select(func.count()).select_from(MetricScore))).scalar_one()
    assert scores == 2  # 2 turns × 1 metric


async def test_score_run_only_scores_selected_turns(session: AsyncSession, registry) -> None:
    # Ingest selects both turns; deselect the second, and scoring must skip it.
    files = [UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default")]
    run_id = await ingest_evaluation("claude", files, session=session)
    second_turn = (
        (await session.execute(select(Turn).order_by(Turn.turn_number))).scalars().all()[1]
    )
    await set_turn_selection(run_id, [str(second_turn.id)], False, session=session)

    await score_run(run_id, session=session, judge=RecordingJudge(0.8))

    # Only the selected turn produced a score row; the other stays unscored.
    scores = (await session.execute(select(func.count()).select_from(MetricScore))).scalar_one()
    assert scores == 1
    by_number = {
        t.turn_number: t.turn_score
        for t in (await session.execute(select(Turn))).scalars().all()
    }
    assert by_number[1] == pytest.approx(0.8)
    assert by_number[2] is None


async def test_set_turn_selection_updates_matching_turns(
    session: AsyncSession, registry
) -> None:
    files = [UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default")]
    run_id = await ingest_evaluation("claude", files, session=session)
    turn_ids = await _all_turn_ids(session, run_id)

    updated = await set_turn_selection(run_id, turn_ids, True, session=session)

    assert updated == 2
    assert all(t.is_selected for t in (await session.execute(select(Turn))).scalars().all())

    # Deselect one turn; the count reflects only the turns actually touched.
    updated = await set_turn_selection(run_id, [turn_ids[0]], False, session=session)
    assert updated == 1
    by_id = {
        str(t.id): t.is_selected
        for t in (await session.execute(select(Turn))).scalars().all()
    }
    assert by_id[turn_ids[0]] is False
    assert by_id[turn_ids[1]] is True


async def test_set_turn_selection_ignores_foreign_and_bad_ids(
    session: AsyncSession, registry
) -> None:
    files = [UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default")]
    run_id = await ingest_evaluation("claude", files, session=session)

    updated = await set_turn_selection(
        run_id,
        ["not-a-uuid", "00000000-0000-0000-0000-000000000000"],
        False,
        session=session,
    )

    assert updated == 0
    # Nothing was touched: the run's own turns keep the selected state ingest gave them.
    assert all(
        t.is_selected for t in (await session.execute(select(Turn))).scalars().all()
    )


async def test_set_turn_selection_unknown_run_returns_none(session: AsyncSession) -> None:
    assert await set_turn_selection("not-a-uuid", [], True, session=session) is None
    assert (
        await set_turn_selection(
            "00000000-0000-0000-0000-000000000000", [], True, session=session
        )
        is None
    )


async def test_set_turn_selection_after_start_raises(session: AsyncSession, registry) -> None:
    files = [UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default")]
    run_id = await ingest_evaluation("claude", files, session=session)
    await start_run(run_id, session=session)  # ingerido -> en_cola

    # Selection is only allowed before a run leaves the 'ingerido' state.
    with pytest.raises(ValueError):
        await set_turn_selection(
            run_id, await _all_turn_ids(session, run_id), True, session=session
        )


async def test_score_run_unknown_id_raises(session: AsyncSession) -> None:
    with pytest.raises(ValueError):
        await score_run("00000000-0000-0000-0000-000000000000", session=session)


async def test_get_run_summary_unknown_returns_none(session: AsyncSession) -> None:
    assert await get_run_summary("not-a-uuid", session=session) is None
    assert await get_run_summary("00000000-0000-0000-0000-000000000000", session=session) is None


async def test_start_run_moves_ingested_to_queued(session: AsyncSession, registry) -> None:
    files = [UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default")]
    run_id = await ingest_evaluation("claude", files, session=session)

    status = await start_run(run_id, session=session)

    assert status == "en_cola"
    run = await session.get(BenchmarkRun, uuid.UUID(run_id))
    assert run.status == "en_cola"


async def test_start_run_unknown_returns_none(session: AsyncSession) -> None:
    assert await start_run("not-a-uuid", session=session) is None
    assert await start_run("00000000-0000-0000-0000-000000000000", session=session) is None


async def test_start_run_already_started_raises(session: AsyncSession, registry) -> None:
    files = [UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default")]
    run_id = await ingest_evaluation("claude", files, session=session)
    await start_run(run_id, session=session)  # ingerido -> en_cola

    # A second start is rejected so a run is never enqueued twice.
    with pytest.raises(ValueError):
        await start_run(run_id, session=session)


# --- prompt binding -----------------------------------------------------------
# Templates come from the database now, so a run must pin the versions it scores
# under: scoring is a long job while the prompt API stays live, and resolving per
# scenario would let an edit split one run's rollups across two rubrics.


class ConPrompt(_FakeMetric):
    """A fake metric that declares a slot, so binding has something to bind."""

    name = "con_prompt"
    prompts = (PromptSlot(slug="verify", required_variables=("claim",)),)

    def evaluate(self, turn: TurnView, judge) -> MetricResult:
        # Sends the *injected* template to the judge, not a class constant — which is
        # what lets a test assert the stored text actually reaches the model.
        self.rubric = safe_format(self.prompt("verify"), claim=turn.response)
        return super().evaluate(turn, judge)


@pytest.fixture
async def registry_with_prompt(session: AsyncSession, compose_use_case):
    saved = MetricRegistry.all()
    MetricRegistry.clear()
    MetricRegistry.add(ConPrompt)
    await compose_use_case([ConPrompt.name])
    try:
        yield
    finally:
        MetricRegistry.clear()
        for metric_cls in saved:
            MetricRegistry.add(metric_cls)


async def _publish(session: AsyncSession, template: str = "Afirmación: {claim}") -> None:
    """Publish an active v1 for every materialized slot — what the migration does."""
    await sync_prompts(session)
    await session.flush()
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
    await session.commit()


async def _ingest_selected(session: AsyncSession) -> str:
    files = [UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default")]
    run_id = await ingest_evaluation("claude", files, session=session)
    await set_turn_selection(
        run_id, await _all_turn_ids(session, run_id), True, session=session
    )
    return run_id


async def test_score_run_binds_the_versions_it_scored_under(
    session: AsyncSession, registry_with_prompt
) -> None:
    run_id = await _ingest_selected(session)
    await _publish(session)

    await score_run(run_id, session=session, judge=RecordingJudge(0.8))

    bindings = (await session.execute(select(RunPromptBinding))).scalars().all()
    active = (
        await session.execute(select(PromptVersion.id).where(PromptVersion.is_active))
    ).scalars().all()
    assert [b.prompt_version_id for b in bindings] == list(active)
    assert {str(b.run_id) for b in bindings} == {run_id}


async def test_rescoring_reuses_the_pinned_bindings(
    session: AsyncSession, registry_with_prompt
) -> None:
    """A run is pinned once. A version published mid-run must not re-pin it.

    Pinning exists so one run's rollups come from one rubric. A re-delivery (or a
    per-turn job) re-enters ``pin_prompts``, so if that re-resolved the *active*
    versions, an edit landing mid-run would split the run across two rubrics —
    precisely what the pin is for. ``uq_run_prompt_binding`` would also reject a blind
    re-insert, so reusing the pin is what keeps re-scoring possible at all.
    """
    run_id = await _ingest_selected(session)
    await _publish(session)
    await score_run(run_id, session=session, judge=RecordingJudge(0.8))
    pinned = (await session.execute(select(RunPromptBinding))).scalars().one()

    # A newer version goes live while the run is under way.
    for version in (await session.execute(select(PromptVersion))).scalars().all():
        version.is_active = False
    for prompt in (await session.execute(select(Prompt))).scalars().all():
        session.add(
            PromptVersion(
                prompt_id=prompt.id,
                version=2,
                template="NUEVA {claim}",
                status="published",
                is_active=True,
            )
        )
    await session.commit()

    # Re-open the scenario and swap the judge model, so the re-run really judges instead
    # of reusing every score — otherwise nothing would reach a rubric to inspect.
    await _rewind(session)
    judge = RecordingJudge(0.6, model="otro-juez")
    seen = _counting(judge)

    await score_run(run_id, session=session, judge=judge)

    count = (
        await session.execute(select(func.count()).select_from(RunPromptBinding))
    ).scalar_one()
    assert count == 1
    binding = (await session.execute(select(RunPromptBinding))).scalars().one()
    assert binding.prompt_version_id == pinned.prompt_version_id  # still v1
    assert seen and not any("NUEVA" in rubric for rubric in seen)


async def test_score_run_injects_the_stored_template(
    session: AsyncSession, registry_with_prompt
) -> None:
    """Editing the stored text changes what the judge receives — the point of the feature."""
    run_id = await _ingest_selected(session)
    await _publish(session, "EDITADA {claim}")
    judge = RecordingJudge(0.8)
    seen: list[str] = []
    original = judge.score

    def _record(*, rubric, turn, scale, rubric_version=None, step=None):
        seen.append(rubric)
        return original(rubric=rubric, turn=turn, scale=scale, rubric_version=rubric_version)

    judge.score = _record

    await score_run(run_id, session=session, judge=judge)

    assert seen and all("EDITADA" in rubric for rubric in seen)


# --- resume -------------------------------------------------------------------


def _counting(judge: RecordingJudge) -> list[str]:
    """Patch ``judge.score`` to record every rubric it is handed, and return the log."""
    seen: list[str] = []
    original = judge.score

    def _record(*, rubric, turn, scale, rubric_version=None, step=None):
        seen.append(rubric)
        return original(rubric=rubric, turn=turn, scale=scale, rubric_version=rubric_version)

    judge.score = _record
    return seen


async def _rewind(session: AsyncSession) -> None:
    """Undo the scenario status a finished run left behind.

    Reproduces a worker that died after committing its scores but before the scenario
    roll-up, which is the state a resumed run actually starts from — and the state in
    which the per-metric fingerprint, rather than the ``completado`` shortcut, decides.
    """
    for scenario in (await session.execute(select(ScenarioResult))).scalars().all():
        scenario.status = "pending"
    await session.commit()


async def test_second_score_run_calls_no_judge(
    session: AsyncSession, registry_with_prompt
) -> None:
    run_id = await _ingest_selected(session)
    await _publish(session)
    await score_run(run_id, session=session, judge=RecordingJudge(0.8))

    judge = RecordingJudge(0.8)
    seen = _counting(judge)
    summary = await score_run(run_id, session=session, judge=judge)

    # The whole point: a re-run over unchanged inputs re-pays nothing.
    assert seen == []
    assert summary["status"] == "completado"
    scores = (await session.execute(select(func.count()).select_from(MetricScore))).scalar_one()
    assert scores == 2


async def test_a_repinned_prompt_version_forces_a_rescore(
    session: AsyncSession, registry_with_prompt
) -> None:
    """Proves the prompt-version id reaches the key: pin -> run_scenario -> run_turn.

    The binding is moved by hand because ``pin_prompts`` binds a run once: publishing v2
    alone leaves this run scoring under the v1 it was pinned to, which is what
    ``test_rescoring_reuses_the_pinned_bindings`` covers.
    """
    run_id = await _ingest_selected(session)
    await _publish(session)
    await score_run(run_id, session=session, judge=RecordingJudge(0.8))
    keys_before = {
        ms.scoring_key for ms in (await session.execute(select(MetricScore))).scalars().all()
    }

    # Supersede v1 with a new active version, as the prompts API does…
    successors: dict[uuid.UUID, PromptVersion] = {}
    for version in (await session.execute(select(PromptVersion))).scalars().all():
        version.is_active = False
        successor = PromptVersion(
            prompt_id=version.prompt_id,
            version=2,
            template="REVISADA {claim}",
            status="published",
            is_active=True,
        )
        session.add(successor)
        successors[version.id] = successor
    await session.flush()
    # …and re-pin the run to it, the only way an already-bound run scores under new text.
    for binding in (await session.execute(select(RunPromptBinding))).scalars().all():
        binding.prompt_version_id = successors[binding.prompt_version_id].id
    await session.commit()
    await _rewind(session)

    judge = RecordingJudge(0.6)
    seen = _counting(judge)
    await score_run(run_id, session=session, judge=judge)

    assert seen and all("REVISADA" in rubric for rubric in seen)
    scores = (await session.execute(select(MetricScore))).scalars().all()
    assert len(scores) == 2  # replaced, not appended
    assert {ms.scoring_key for ms in scores}.isdisjoint(keys_before)


async def test_score_run_fails_fast_when_a_slot_has_no_active_version(
    session: AsyncSession, registry_with_prompt
) -> None:
    """Better a terminal ``fallido`` than a run that quietly scores nothing.

    Left to ``Metric.prompt``, the error would surface on a judge worker thread where
    skip-metric-continue logs a warning and drops the metric — the run would report
    success having measured nothing.
    """
    run_id = await _ingest_selected(session)
    await sync_prompts(session)
    await session.commit()  # slots exist, but no version was ever published

    with pytest.raises(MissingPromptError, match="con_prompt.verify"):
        await score_run(run_id, session=session, judge=RecordingJudge(0.8))

    assert (await session.get(BenchmarkRun, uuid.UUID(run_id))).status == "fallido"
    scores = (await session.execute(select(func.count()).select_from(MetricScore))).scalar_one()
    assert scores == 0
