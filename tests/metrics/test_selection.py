"""DB-backed per-scenario selection, synced from the decorator-declared classes.

``hallucination`` declares no scenarios, so it belongs to the reserved
``"default"`` set and every scenario without its own rows resolves to it via the
fallback. ``faithfulness_ragas``/``faithfulness_deepeval`` declare
``["document_retrieval", "web_search"]`` and materialize under those use cases.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from scorekeeper.database import ScenarioMetric
from scorekeeper.metrics.base import Metric
from scorekeeper.metrics.selection import (
    metrics_for,
    resolve,
    sync_selection,
)

DEFAULT_METRICS = {"hallucination"}
RETRIEVAL_METRICS = {"faithfulness_ragas", "faithfulness_deepeval"}


def test_sync_materializes_declared_scenarios(db_session: Session, registered_metrics) -> None:
    sync_selection(db_session)
    db_session.commit()

    # hallucination declares no scenarios → it lands in the "default" set.
    assert set(metrics_for(db_session, "default")) == DEFAULT_METRICS
    # The faithfulness metrics materialize under their declared use cases.
    assert set(metrics_for(db_session, "document_retrieval")) == RETRIEVAL_METRICS
    assert set(metrics_for(db_session, "web_search")) == RETRIEVAL_METRICS


def test_sync_is_idempotent(db_session: Session, registered_metrics) -> None:
    sync_selection(db_session)
    db_session.commit()
    count_1 = db_session.execute(select(func.count()).select_from(ScenarioMetric)).scalar_one()

    sync_selection(db_session)
    db_session.commit()
    count_2 = db_session.execute(select(func.count()).select_from(ScenarioMetric)).scalar_one()

    assert count_1 == count_2


def test_sync_removes_stale_rows(db_session: Session, registered_metrics) -> None:
    db_session.add(ScenarioMetric(use_case="default", metric_name="metrica_retirada"))
    db_session.commit()

    sync_selection(db_session)
    db_session.commit()

    assert "metrica_retirada" not in metrics_for(db_session, "default")


def test_unknown_use_case_falls_back_to_default(db_session: Session, registered_metrics) -> None:
    sync_selection(db_session)
    db_session.commit()
    # A scenario with no rows of its own falls back to the "default" set.
    assert set(metrics_for(db_session, "escenario_inexistente")) == DEFAULT_METRICS


def test_resolve_returns_metric_instances(db_session: Session, registered_metrics) -> None:
    sync_selection(db_session)
    db_session.commit()

    metrics = resolve(db_session, "default")
    assert all(isinstance(m, Metric) for m in metrics)
    assert {m.name for m in metrics} == DEFAULT_METRICS


def test_resolve_raises_on_unknown_stored_metric(db_session: Session, registered_metrics) -> None:
    db_session.add(ScenarioMetric(use_case="raro", metric_name="fantasma"))
    db_session.commit()

    with pytest.raises(KeyError, match="Métrica desconocida"):
        resolve(db_session, "raro")
