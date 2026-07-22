"""Tests for the structlog-based dual-output logging configuration.

``configure_logging`` must install exactly two root handlers — plain text on
stderr (console) and JSON on stdout (telemetry) — and route both native structlog
events and third-party ``logging`` records through them. We drive real streams and
assert on what each one emits.
"""

from __future__ import annotations

import io
import json
import logging

import pytest
import structlog

from scorekeeper import logging_config


@pytest.fixture(autouse=True)
def _reset_logging():
    """Restore root handlers/level and the configure guard around each test."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_guard = logging_config._configured
    try:
        yield
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        logging_config._configured = saved_guard
        structlog.reset_defaults()


def _capture(monkeypatch: pytest.MonkeyPatch) -> tuple[io.StringIO, io.StringIO]:
    """Point the two handlers at in-memory streams and return (stderr, stdout)."""
    import sys

    err, out = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(sys, "stdout", out)
    logging_config.configure_logging(force=True)
    return err, out


def test_installs_two_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch)
    assert len(logging.getLogger().handlers) == 2


def test_structlog_event_goes_to_both_console_and_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    err, out = _capture(monkeypatch)
    structlog.get_logger("scorekeeper.test").info("llm_call", op="score", latency_ms=12.3)

    console = err.getvalue()
    telemetry = out.getvalue()

    # Console (stderr): human-readable plain text with the event and its fields.
    assert "llm_call" in console
    assert "op=score" in console
    # Telemetry (stdout): one JSON object carrying the same fields.
    record = json.loads(telemetry)
    assert record["event"] == "llm_call"
    assert record["op"] == "score"
    assert record["latency_ms"] == 12.3
    assert record["level"] == "info"
    assert "timestamp" in record


def test_stdlib_record_is_routed_through_structlog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A plain logging record (e.g. from a third-party library) renders through the
    # same handlers: JSON on stdout, text on stderr.
    err, out = _capture(monkeypatch)
    logging.getLogger("some.library").warning("plain message")

    assert "plain message" in err.getvalue()
    record = json.loads(out.getvalue())
    assert record["event"] == "plain message"
    assert record["level"] == "warning"


def test_idempotent_without_force(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch)
    # A second call without force is a no-op: still exactly two handlers.
    logging_config.configure_logging()
    assert len(logging.getLogger().handlers) == 2
