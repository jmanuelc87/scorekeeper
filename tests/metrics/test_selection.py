"""DB-backed per-scenario selection, synced from the decorator-declared classes."""

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


def test_sync_materializes_declared_scenarios(db_session: Session, registered_metrics) -> None:
    sync_selection(db_session)
    db_session.commit()

    assert set(metrics_for(db_session, "soporte_tecnico")) == {
        "correccion",
        "utilidad",
        "seguridad_factual",
    }
    assert set(metrics_for(db_session, "ventas")) == {"correccion", "utilidad", "tono"}


def test_sync_is_idempotent(db_session: Session, registered_metrics) -> None:
    sync_selection(db_session)
    db_session.commit()
    count_1 = db_session.execute(select(func.count()).select_from(ScenarioMetric)).scalar_one()

    sync_selection(db_session)
    db_session.commit()
    count_2 = db_session.execute(select(func.count()).select_from(ScenarioMetric)).scalar_one()

    assert count_1 == count_2


def test_sync_removes_stale_rows(db_session: Session, registered_metrics) -> None:
    db_session.add(ScenarioMetric(use_case="ventas", metric_name="metrica_retirada"))
    db_session.commit()

    sync_selection(db_session)
    db_session.commit()

    assert "metrica_retirada" not in metrics_for(db_session, "ventas")


def test_unknown_use_case_falls_back_to_default(db_session: Session, registered_metrics) -> None:
    sync_selection(db_session)
    db_session.commit()
    # No catalog metric targets "default", so the fallback is empty.
    assert metrics_for(db_session, "escenario_inexistente") == []


def test_resolve_returns_metric_instances(db_session: Session, registered_metrics) -> None:
    sync_selection(db_session)
    db_session.commit()

    metrics = resolve(db_session, "ventas")
    assert all(isinstance(m, Metric) for m in metrics)
    assert {m.name for m in metrics} == {"correccion", "utilidad", "tono"}


def test_resolve_raises_on_unknown_stored_metric(db_session: Session, registered_metrics) -> None:
    db_session.add(ScenarioMetric(use_case="raro", metric_name="fantasma"))
    db_session.commit()

    with pytest.raises(KeyError, match="Métrica desconocida"):
        resolve(db_session, "raro")
