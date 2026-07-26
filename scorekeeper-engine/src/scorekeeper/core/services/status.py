"""The run statuses that precede scoring.

The scoring-complete literals — ``completado`` / ``parcial`` / ``fallido`` —
belong to the runner and live in :mod:`scorekeeper.core.runner`.
"""

from __future__ import annotations

STATUS_INGERIDO = "ingerido"  # persisted, not yet started
STATUS_EN_COLA = "en_cola"  # enqueued to Celery, waiting for a worker
STATUS_EN_PROCESO = "en_proceso"  # a worker is scoring it now
