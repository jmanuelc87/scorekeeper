"""Structured logging for the whole app, built on :mod:`structlog`.

Every process emits **two outputs at once** from a single log call:

* a human-readable **plain-text** line to **stderr** (the console), colorized when
  stderr is a TTY; and
* a **JSON** line to **stdout** (telemetry), one object per event, for a log
  shipper/collector to ingest.

Both share one processor chain (timestamp, level, contextvars), so a structlog
event and a plain ``logging`` record from a third-party library (``httpx``,
``uvicorn``, ``celery``) render identically through both handlers. structlog is the
default: application code should ``structlog.get_logger(__name__)`` and log with
key/value pairs (``log.info("llm_call", op="score", latency_ms=812.4)``); those
keys become columns in the JSON output and ``key=value`` pairs on the console.

Call :func:`configure_logging` once per process at startup (the API does it on
import; the Celery worker does it from its logging-setup signal with ``force``).
It is idempotent: it resets the root handlers each time it actually runs.
"""

from __future__ import annotations

import logging
import sys

import structlog

# Guard so repeated imports/calls in one process don't stack duplicate handlers.
_configured = False


def _shared_processors() -> list:
    """Processors applied to BOTH structlog events and stdlib log records.

    Kept as the ``foreign_pre_chain`` for stdlib records and as the front of the
    structlog chain, so a third-party ``logging`` record carries the same
    timestamp/level/context fields as a native structlog event.
    """
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]


def configure_logging(level: int | str = logging.INFO, *, force: bool = False) -> None:
    """Configure structlog + stdlib for dual (console text + JSON telemetry) output.

    ``level`` sets the root log level. ``force`` re-applies the configuration even
    if it already ran in this process — used by the Celery worker, which must
    reconfigure *after* Celery has set up (and possibly hijacked) logging on boot.
    """
    global _configured
    if _configured and not force:
        return

    shared = _shared_processors()

    # Route structlog through stdlib logging so its records reach the same handlers
    # as third-party (``logging``) records; ProcessorFormatter renders both.
    structlog.configure(
        processors=[
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Plain text → stderr (console). Colorized only on a TTY.
    console_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
        ],
    )
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(console_formatter)

    # JSON → stdout (telemetry). Tracebacks render as structured data, not a blob.
    json_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
    )
    json_handler = logging.StreamHandler(sys.stdout)
    json_handler.setFormatter(json_formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(console_handler)
    root.addHandler(json_handler)
    root.setLevel(level)

    # Each judge call makes httpx log the request line at INFO; that floods both
    # outputs (one line per metric per turn). Our own events are the signal we want.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    _configured = True
