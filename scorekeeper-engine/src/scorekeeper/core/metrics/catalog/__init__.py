"""Metric catalog — the home for concrete evaluation metrics.

This package is intentionally empty. Add one module per metric here, each
defining a ``Metric`` subclass decorated with ``@register`` (see
``docs/evaluation-metrics.md``), then import it below so that importing this
package registers every metric::

    from scorekeeper.core.metrics.catalog import correccion, utilidad  # noqa: F401
"""

from scorekeeper.core.metrics.catalog import faithfulness  # noqa: F401
from scorekeeper.core.metrics.catalog import hallucination  # noqa: F401
from scorekeeper.core.metrics.catalog import answer_relevance  # noqa: F401
from scorekeeper.core.metrics.catalog import contextual_precision  # noqa: F401
from scorekeeper.core.metrics.catalog import relevancia  # noqa: F401
from scorekeeper.core.metrics.catalog import precision  # noqa: F401
from scorekeeper.core.metrics.catalog import completitud  # noqa: F401
from scorekeeper.core.metrics.catalog import claridad  # noqa: F401
from scorekeeper.core.metrics.catalog import razonamiento_logico  # noqa: F401
from scorekeeper.core.metrics.catalog import contextualizacion  # noqa: F401
from scorekeeper.core.metrics.catalog import accionabilidad  # noqa: F401
from scorekeeper.core.metrics.catalog import estructura  # noqa: F401
from scorekeeper.core.metrics.catalog import profundidad_analitica  # noqa: F401
from scorekeeper.core.metrics.catalog import coherencia_multiturno  # noqa: F401
