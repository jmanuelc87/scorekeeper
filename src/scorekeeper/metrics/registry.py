"""Runtime registry of metric classes.

Concrete metrics register themselves with the ``@register`` decorator, co-located
with their definition. Importing the ``catalog`` package imports every metric
module, which populates this registry. Per-scenario selection is then derived by
*querying these registered classes at runtime* (see ``selection.sync_selection``)
and storing the result in the ``scenario_metrics`` table.

The decorator optionally carries the scenarios a metric applies to::

    @register(scenarios=["soporte_tecnico", "ventas"])
    class Correccion(SingleRubricMetric):
        ...

    @register  # applies to the "default" selection only
    class Utilidad(SingleRubricMetric):
        ...
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TypeVar, overload

from scorekeeper.metrics.base import Metric

# Preserve the concrete metric subclass through the decorator so callers (and type
# checkers) still see class-specific attributes, e.g. FaithfulnessRagas.bulk_model,
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
    def create(cls, name: str) -> Metric:
        return cls.get(name)()

    @classmethod
    def all(cls) -> list[type[Metric]]:
        return list(cls._metrics.values())

    @classmethod
    def clear(cls) -> None:
        """Reset the registry — primarily for isolated tests."""
        cls._metrics.clear()


@overload
def register(metric_cls: type[_M]) -> type[_M]: ...


@overload
def register(
    *, scenarios: Iterable[str] | None = ...
) -> Callable[[type[_M]], type[_M]]: ...


def register(
    metric_cls: type[_M] | None = None,
    *,
    scenarios: Iterable[str] | None = None,
) -> type[_M] | Callable[[type[_M]], type[_M]]:
    """Register a metric class, optionally tagging the scenarios it applies to.

    Usable bare (``@register``) or parameterized (``@register(scenarios=[...])``).
    Scenario tags passed here override a class-level ``scenarios`` attribute.
    """

    def wrap(cls: type[_M]) -> type[_M]:
        if scenarios is not None:
            cls.scenarios = tuple(scenarios)
        MetricRegistry.add(cls)
        return cls

    return wrap if metric_cls is None else wrap(metric_cls)
