"""Tests for the structlog-based plain-text logging configuration.

``configure_logging`` must install exactly one root handler that emits
human-readable plain text to stdout (no JSON stream, no separate stderr stream),
routing both native structlog events and third-party ``logging`` records through
it. We drive real streams and assert on what each one emits.
"""

from __future__ import annotations

import io
import logging

import pytest
import structlog

from scorekeeper.utils import logging_config


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
    """Point stdout/stderr at in-memory streams and return (stderr, stdout)."""
    import sys

    err, out = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(sys, "stdout", out)
    logging_config.configure_logging(force=True)
    return err, out


def test_installs_single_stdout_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch)
    assert len(logging.getLogger().handlers) == 1


def test_structlog_event_is_plain_text_on_stdout_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    err, out = _capture(monkeypatch)
    structlog.get_logger("scorekeeper.test").info("llm_call", op="score", latency_ms=12.3)

    # Nothing on the console (stderr); a single human-readable line on stdout with the
    # event and its key=value fields (not JSON).
    assert err.getvalue() == ""
    line = out.getvalue()
    assert "llm_call" in line
    assert "op=score" in line
    assert "latency_ms=12.3" in line
    assert not line.lstrip().startswith("{")


def test_stdlib_record_is_routed_through_structlog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A plain logging record (e.g. from a third-party library) renders as text on
    # stdout through the same handler.
    err, out = _capture(monkeypatch)
    logging.getLogger("some.library").warning("plain message")

    assert err.getvalue() == ""
    assert "plain message" in out.getvalue()


def test_idempotent_without_force(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch)
    # A second call without force is a no-op: still exactly one handler.
    logging_config.configure_logging()
    assert len(logging.getLogger().handlers) == 1
