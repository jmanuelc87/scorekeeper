"""Metric categories — data-only grouping for reporting and dashboards."""

from __future__ import annotations

from enum import StrEnum


class MetricCategory(StrEnum):
    """Groups metrics for aggregation/filtering. Carries no behavior."""

    CALIDAD = "calidad"
    COMUNICACION = "comunicacion"
    SEGURIDAD = "seguridad"
