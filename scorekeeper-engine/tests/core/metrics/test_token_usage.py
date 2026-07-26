"""Unit tests for the SDK-free token-usage collector primitives.

These cover the value types and the context-scoped accumulator that the concrete
judges push each call's usage into. The critical property is thread isolation with
reset: a ThreadPoolExecutor worker (which does NOT inherit the submitter's context
and is reused across tasks) must scope its own accumulator and leave the context var
clean afterward, so one metric's tokens never leak into another's.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from scorekeeper.core.metrics.judges.base import (
    TokenUsage,
    UsageAccumulator,
    _usage_var,
    collect_usage,
    record_usage,
)


def test_token_usage_add_and_total() -> None:
    combined = TokenUsage(input_tokens=3, output_tokens=2) + TokenUsage(
        input_tokens=10, output_tokens=1
    )
    assert (combined.input_tokens, combined.output_tokens) == (13, 3)
    assert combined.total_tokens == 16


def test_accumulator_sums_across_calls() -> None:
    acc = UsageAccumulator()
    with collect_usage(acc):
        record_usage(input_tokens=5, output_tokens=1)
        record_usage(input_tokens=2, output_tokens=3)
        record_usage(input_tokens=None, output_tokens=None)  # None → 0, no error
    snap = acc.snapshot()
    assert (snap.input_tokens, snap.output_tokens) == (7, 4)


def test_record_usage_is_noop_without_scope() -> None:
    # With no active accumulator, recording must not raise and must record nothing.
    assert _usage_var.get() is None
    record_usage(input_tokens=99, output_tokens=99)
    assert _usage_var.get() is None


def test_collect_usage_resets_on_exit_and_on_error() -> None:
    acc = UsageAccumulator()
    with collect_usage(acc):
        assert _usage_var.get() is acc
    assert _usage_var.get() is None  # reset on normal exit

    try:
        with collect_usage(UsageAccumulator()):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert _usage_var.get() is None  # reset even when the block raises


def test_accumulators_are_isolated_across_reused_threads() -> None:
    # max_workers=1 forces the two tasks onto the SAME reused worker thread. Each
    # scopes its own accumulator; the var seen at entry and after exit must be None
    # (proving reset prevents leakage between tasks on a reused thread).
    def task(n: int) -> tuple[object, int, object]:
        acc = UsageAccumulator()
        seen_before = _usage_var.get()
        with collect_usage(acc):
            record_usage(input_tokens=n, output_tokens=0)
        return seen_before, acc.snapshot().input_tokens, _usage_var.get()

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(task, 11).result()
        second = pool.submit(task, 22).result()

    assert first == (None, 11, None)
    assert second == (None, 22, None)
