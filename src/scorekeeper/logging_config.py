"""Structured logging for the whole app, built on :mod:`structlog`.

Every process emits a **single plain-text stream** to **stdout** — one
human-readable line per event, colorized only when stdout is a TTY. There is no
JSON stream and no separate stderr stream: in a container stdout and stderr merge
into one ``docker logs`` output, so a second copy would duplicate every event.

The processor chain (timestamp, level, contextvars) is shared between structlog
events and plain ``logging`` records, so a third-party ``logging`` record (``httpx``,
``uvicorn``, ``celery``) renders identically to a native structlog event. structlog
is the default: application code should ``structlog.get_logger(__name__)`` and log
with key/value pairs (``log.info("llm_call", op="score", latency_ms=812.4)``); those
keys render as ``key=value`` pairs on the line.

Call :func:`configure_logging` once per process at startup (the API does it on
import; the Celery worker does it from its logging-setup signal with ``force``).
It is idempotent: it resets the root handlers each time it actually runs.

The one caller that overrides the target ``stream`` is the MCP server under the
``stdio`` transport: stdout there carries the JSON-RPC protocol, so its logs go to
stderr instead to avoid corrupting the wire (see :mod:`scorekeeper.server`).
"""

from __future__ import annotations

import logging
import sys
from typing import IO

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


def configure_logging(
    level: int | str = logging.INFO,
    *,
    force: bool = False,
    stream: IO[str] | None = None,
) -> None:
    """Configure structlog + stdlib to emit plain-text logs to a single stream.

    ``level`` sets the root log level. ``force`` re-applies the configuration even
    if it already ran in this process — used by the Celery worker, which must
    reconfigure *after* Celery has set up (and possibly hijacked) logging on boot.

    ``stream`` is the target for the output; ``None`` (default) means stdout. The
    MCP server passes ``sys.stderr`` under the stdio transport, whose stdout is the
    JSON-RPC channel.
    """
    global _configured
    if _configured and not force:
        return

    target = stream if stream is not None else sys.stdout
    shared = _shared_processors()

    # Route structlog through stdlib logging so its records reach the same handler
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

    # Human-readable plain text, one line per event. Colorized only on a TTY (so a
    # container's redirected stdout stays clean, uncolored text).
    console_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(colors=target.isatty()),
        ],
    )
    console_handler = logging.StreamHandler(target)
    console_handler.setFormatter(console_formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(console_handler)
    root.setLevel(level)

    # Each judge call makes httpx log the request line at INFO; that floods the
    # output (one line per metric per turn). Our own events are the signal we want.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    _configured = True
