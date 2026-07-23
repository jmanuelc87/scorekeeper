"""Evaluation-metric taxonomy.

Importing this package registers every catalog metric, so downstream code can
resolve metrics by name and derive per-scenario selection from the registered
classes.
"""

from scorekeeper.metrics import catalog  # noqa: F401  (registers metrics on import)
from scorekeeper.metrics.base import (
    Metric,
    MetricResult,
    MetricTrace,
    MultiStepMetric,
    SingleRubricMetric,
    TraceEntry,
    TraceStep,
    TurnView,
)
from scorekeeper.metrics.category import MetricCategory
from scorekeeper.metrics.judge import Judge, JudgeVerdict
from scorekeeper.metrics.registry import MetricRegistry, register
from scorekeeper.metrics.scale import Boolean, Likert, Scale, Unit

__all__ = [
    "Boolean",
    "Judge",
    "JudgeVerdict",
    "Likert",
    "Metric",
    "MetricCategory",
    "MetricRegistry",
    "MetricResult",
    "MetricTrace",
    "MultiStepMetric",
    "Scale",
    "SingleRubricMetric",
    "TraceEntry",
    "TraceStep",
    "TurnView",
    "Unit",
    "register",
]
