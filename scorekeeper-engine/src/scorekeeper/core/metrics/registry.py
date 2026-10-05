"""Runtime registry of metric classes.

Concrete metrics register themselves with the ``@register`` decorator, co-located
with their definition. Importing the ``catalog`` package imports every metric
module, which populates this registry::

    @register
    class Utilidad(SingleRubricMetric):
        ...

The registry is the catalog of what *can* be scored. Which metrics a given use
case actually scores is user data, composed through ``POST /use-cases`` and
stored in ``use_case_metrics``; ``selection.sync_metrics`` mirrors the names
registered here into the ``metrics`` table so those links have a key to point at.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypeVar

from scorekeeper.core.metrics.base import Metric

# Preserve the concrete metric subclass through the decorator so callers (and type
# checkers) still see class-specific attributes, e.g. FaithfulnessDeepeval.truths_model,
# rather than an erased ``type[Metric]``.
_M = TypeVar("_M", bound=Metric)


class MetricRegistry:
    """A name → metric-class registry populated at import time."""

    _metrics: dict[str, type[Metric]] = {}

    @classmethod
    def add(cls, metric_cls: type[Metric]) -> None:
        name = getattr(metric_cls, "name", None)
        if not name:
            raise ValueError(f"La métrica {metric_cls.__name__} no define 'name'")
        existing = cls._metrics.get(name)
        if existing is not None and existing is not metric_cls:
            raise ValueError(f"Métrica duplicada: {name}")
        cls._metrics[name] = metric_cls

    @classmethod
    def get(cls, name: str) -> type[Metric]:
        try:
            return cls._metrics[name]
        except KeyError:
            raise KeyError(f"Métrica desconocida: {name}") from None

    @classmethod
    def create(cls, name: str, templates: Mapping[str, str] | None = None) -> Metric:
        """Instantiate ``name``, binding ``templates`` to its declared prompt slots."""
        return cls.get(name)(templates)

    @classmethod
    def all(cls) -> list[type[Metric]]:
        return list(cls._metrics.values())

    @classmethod
    def clear(cls) -> None:
        """Reset the registry — primarily for isolated tests."""
        cls._metrics.clear()


def register(metric_cls: type[_M]) -> type[_M]:
    """Register a metric class so the catalog and the judges can find it by name."""
    MetricRegistry.add(metric_cls)
    return metric_cls
