"""Metric catalog — the home for concrete evaluation metrics.

This package is intentionally empty. Add one module per metric here, each
defining a ``Metric`` subclass decorated with ``@register`` (see
``docs/evaluation-metrics.md``), then import it below so that importing this
package registers every metric::

    from scorekeeper.metrics.catalog import correccion, utilidad  # noqa: F401
"""

from scorekeeper.metrics.catalog import faithfulness  # noqa: F401
