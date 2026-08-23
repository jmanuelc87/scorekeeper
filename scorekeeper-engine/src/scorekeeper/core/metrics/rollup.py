"""Score roll-up.

Turn score is the *weighted mean of normalized* metric scores — refining the
plain mean documented in ``docs/data-model.md``. Metrics don't share a scale, so
each raw ``MetricScore.score`` is normalized to [0, 1] via its metric's ``scale``
(looked up in the registry by ``metric_name``) and weighted by the metric's
``weight``. Scenario and platform averages are plain means of their already-
normalized children.

Every mean here skips the ``NOT_APPLICABLE`` sentinel: a metric that had nothing
to measure (no retrieved context, no claims) reports a negative score instead of
inventing a 0.0 or a 1.0, and an unmeasured child must not move an average.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

import numpy as np

from scorekeeper.core.metrics.base import is_not_applicable
from scorekeeper.core.metrics.registry import MetricRegistry


class _Scored(Protocol):
    """Anything carrying a ``metric_name`` and a raw ``score`` (e.g. MetricScore)."""

    metric_name: str
    score: float


def turn_score(scores: Iterable[_Scored]) -> float | None:
    """Weighted mean of normalized metric scores for one turn.

    Not-applicable scores are skipped before normalizing — a sentinel must never
    reach ``scale.normalize`` (``Inverted`` would map ``-1.0`` to ``2.0``) — and
    carry no weight, so the mean is over the metrics that actually measured
    something. Returns ``None`` when there are no such scores. Raises ``KeyError``
    if a ``metric_name`` is not in the registry.
    """
    numerator = 0.0
    denominator = 0.0
    for score in scores:
        if is_not_applicable(score.score):
            continue
        metric = MetricRegistry.get(score.metric_name)
        numerator += metric.scale.normalize(score.score) * metric.weight
        denominator += metric.weight
    return numerator / denominator if denominator else None


def average(values: Iterable[float | None]) -> float | None:
    """Plain mean of the children that carry a score.

    ``None`` (never scored) and negative values (:data:`NOT_APPLICABLE`, nothing to
    measure) are dropped; everything else counts, including a legitimate ``0.0`` or
    ``1.0``. Returns ``None`` if nothing remains.
    """
    present = np.array(
        [v for v in values if v is not None and not is_not_applicable(v)],
        dtype=float,
    )
    return float(np.mean(present)) if present.size else None


# Both rollups above a turn are the same plain mean of their children:
# ``execution_average`` folds a conversation's turn scores into its PlatformExecution,
# ``platform_average`` folds a run's executions into one per-platform figure. There is no
# scenario average — a mean across the platforms being compared is not a useful number.
execution_average = average
platform_average = average
