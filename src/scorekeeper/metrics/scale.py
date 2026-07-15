"""Score scales and their normalization to a common [0, 1] range.

Metrics score on their own scale (1-5 Likert, 0-1 unit, boolean pass/fail). The
raw score is what gets stored; normalization to [0, 1] happens only at rollup so
heterogeneous metrics can be averaged fairly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class Scale(ABC):
    """A score scale that knows how to normalize a raw score to [0, 1]."""

    @abstractmethod
    def normalize(self, raw: float) -> float:
        """Map a raw score onto [0, 1]."""


class Likert(Scale):
    """A discrete/continuous scale between ``lo`` and ``hi`` (default 1-5)."""

    def __init__(self, lo: float = 1.0, hi: float = 5.0) -> None:
        if hi <= lo:
            raise ValueError("El límite superior de la escala debe ser mayor que el inferior")
        self.lo = lo
        self.hi = hi

    def normalize(self, raw: float) -> float:
        return (raw - self.lo) / (self.hi - self.lo)


class Unit(Scale):
    """A scale already expressed in [0, 1]."""

    def normalize(self, raw: float) -> float:
        return raw


class Boolean(Scale):
    """A pass/fail scale stored as 0.0 / 1.0."""

    def normalize(self, raw: float) -> float:
        return 1.0 if raw >= 0.5 else 0.0
