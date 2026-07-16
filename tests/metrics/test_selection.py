"""DB-backed per-scenario selection, synced from the decorator-declared classes.

Each catalog metric declares the use case(s) it applies to via
``@register(scenarios=…)``: ``contextual_precision`` → ``["contextual_precision"]``,
``hallucination`` → ``["hallucination"]``, and
``faithfulness_ragas``/``faithfulness_deepeval`` → ``["document_retrieval",
"web_search"]``. None declare the reserved ``"default"`` set, so a scenario resolves
to metrics only when its ``use_case`` names one; an all-miss ``use_case`` falls back
to whatever the ``"default"`` set contains (empty for the catalog metrics, so these
tests seed a ``"default"`` row to exercise the fallback).
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from scorekeeper.database import ScenarioMetric
from scorekeeper.metrics.base import Metric
from scorekeeper.metrics.selection import (
    metrics_for,
    metrics_for_scenario,
    parse_use_cases,
    resolve,
    resolve_scenario,
    sync_selection,
)

CTX_METRICS = {"contextual_precision"}
RETRIEVAL_METRICS = {"faithfulness_ragas", "faithfulness_deepeval"}
HALLUCINATION_METRICS = {"hallucination"}


def test_sync_materializes_declared_scenarios(db_session: Session, registered_metrics) -> None:
    sync_selection(db_session)
    db_session.commit()

    # Each metric materializes under its own declared use case.
    assert set(metrics_for(db_session, "contextual_precision")) == CTX_METRICS
    assert set(metrics_for(db_session, "hallucination")) == HALLUCINATION_METRICS
    assert set(metrics_for(db_session, "document_retrieval")) == RETRIEVAL_METRICS
    assert set(metrics_for(db_session, "web_search")) == RETRIEVAL_METRICS
    # No catalog metric declares the reserved "default" set.
    assert set(metrics_for(db_session, "default")) == set()


def test_sync_is_idempotent(db_session: Session, registered_metrics) -> None:
    sync_selection(db_session)
    db_session.commit()
    count_1 = db_session.execute(select(func.count()).select_from(ScenarioMetric)).scalar_one()

    sync_selection(db_session)
    db_session.commit()
    count_2 = db_session.execute(select(func.count()).select_from(ScenarioMetric)).scalar_one()

    assert count_1 == count_2


def test_sync_removes_stale_rows(db_session: Session, registered_metrics) -> None:
    db_session.add(ScenarioMetric(use_case="contextual_precision", metric_name="metrica_retirada"))
    db_session.commit()

    sync_selection(db_session)
    db_session.commit()

    assert "metrica_retirada" not in metrics_for(db_session, "contextual_precision")


def test_unknown_use_case_falls_back_to_default(db_session: Session, registered_metrics) -> None:
    sync_selection(db_session)
    # No catalog metric declares "default", so seed one to exercise the fallback.
    db_session.add(ScenarioMetric(use_case="default", metric_name="contextual_precision"))
    db_session.commit()

    # A scenario with no rows of its own falls back to the "default" set.
    assert set(metrics_for(db_session, "escenario_inexistente")) == CTX_METRICS


def test_resolve_returns_metric_instances(db_session: Session, registered_metrics) -> None:
    sync_selection(db_session)
    db_session.commit()

    metrics = resolve(db_session, "contextual_precision")
    assert all(isinstance(m, Metric) for m in metrics)
    assert {m.name for m in metrics} == CTX_METRICS


def test_resolve_raises_on_unknown_stored_metric(db_session: Session, registered_metrics) -> None:
    db_session.add(ScenarioMetric(use_case="raro", metric_name="fantasma"))
    db_session.commit()

    with pytest.raises(KeyError, match="Métrica desconocida"):
        resolve(db_session, "raro")


# --- Comma-separated (per-scenario) selection ---------------------------------


def test_parse_use_cases_splits_and_strips() -> None:
    assert parse_use_cases(" document_retrieval ,, web_search ") == [
        "document_retrieval",
        "web_search",
    ]
    assert parse_use_cases("default") == ["default"]
    assert parse_use_cases("  ,  ") == []


def test_metrics_for_scenario_unions_tokens(db_session: Session, registered_metrics) -> None:
    sync_selection(db_session)
    db_session.commit()

    names = metrics_for_scenario(db_session, "document_retrieval, contextual_precision")
    assert set(names) == RETRIEVAL_METRICS | CTX_METRICS
    # No duplicates in the union.
    assert len(names) == len(set(names))


def test_metrics_for_scenario_dedups_repeated_tokens(
    db_session: Session, registered_metrics
) -> None:
    sync_selection(db_session)
    db_session.commit()

    names = metrics_for_scenario(db_session, "document_retrieval, document_retrieval, web_search")
    assert names == sorted(RETRIEVAL_METRICS)  # each metric once, name-ordered


def test_metrics_for_scenario_ignores_whitespace_and_empty_tokens(
    db_session: Session, registered_metrics
) -> None:
    sync_selection(db_session)
    db_session.commit()

    assert set(metrics_for_scenario(db_session, " document_retrieval ,, web_search ")) == (
        RETRIEVAL_METRICS
    )


def test_metrics_for_scenario_falls_back_only_when_all_miss(
    db_session: Session, registered_metrics
) -> None:
    sync_selection(db_session)
    # No catalog metric declares "default", so seed one to exercise the fallback.
    db_session.add(ScenarioMetric(use_case="default", metric_name="contextual_precision"))
    db_session.commit()

    # Every token misses → default set.
    assert set(metrics_for_scenario(db_session, "inexistente, otro_raro")) == CTX_METRICS
    # One token matches → no default injected.
    assert set(metrics_for_scenario(db_session, "inexistente, document_retrieval")) == (
        RETRIEVAL_METRICS
    )


def test_resolve_scenario_returns_union_instances(
    db_session: Session, registered_metrics
) -> None:
    sync_selection(db_session)
    db_session.commit()

    metrics = resolve_scenario(db_session, "document_retrieval, contextual_precision")
    assert all(isinstance(m, Metric) for m in metrics)
    assert {m.name for m in metrics} == RETRIEVAL_METRICS | CTX_METRICS
