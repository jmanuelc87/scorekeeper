"""Tests for the overlapping sentence chunker."""

from __future__ import annotations

import pytest

from scorekeeper.core.retrieval.chunk import chunk_sentences
from scorekeeper.core.retrieved_context import Sentence


def _sentences(n: int, *, pages: list[int | None] | None = None) -> list[Sentence]:
    """``n`` sentences, all on page 1 unless ``pages`` gives one per sentence."""
    return [
        Sentence(page=1 if pages is None else pages[i], index=i, text=f"s{i}")
        for i in range(n)
    ]


def _texts(sentences, **kwargs) -> list[str]:
    return [window.text for window in chunk_sentences(sentences, **kwargs)]


def test_windows_overlap_by_the_requested_amount() -> None:
    # 7 sentences, windows of 3 sharing 1 → step 2, and the last window is short.
    assert _texts(_sentences(7), size=3, overlap=1) == [
        "s0 s1 s2",
        "s2 s3 s4",
        "s4 s5 s6",
    ]


def test_zero_overlap_partitions_without_repeating() -> None:
    assert _texts(_sentences(6), size=3, overlap=0) == ["s0 s1 s2", "s3 s4 s5"]


def test_last_window_may_be_short() -> None:
    assert _texts(_sentences(4), size=3, overlap=0) == ["s0 s1 s2", "s3"]


def test_a_tail_already_covered_is_not_repeated() -> None:
    """With 5 sentences, size 3, overlap 2 the step is 1 — but 's4' alone adds nothing."""
    assert _texts(_sentences(5), size=3, overlap=2) == ["s0 s1 s2", "s1 s2 s3", "s2 s3 s4"]


def test_fewer_sentences_than_a_window_yields_one_chunk() -> None:
    assert _texts(_sentences(2), size=5, overlap=1) == ["s0 s1"]


def test_no_sentences_yields_no_chunks() -> None:
    assert chunk_sentences([], size=3, overlap=1) == []


@pytest.mark.parametrize("size, overlap", [(3, 3), (3, 4), (1, 1)])
def test_overlap_that_would_never_advance_is_rejected(size: int, overlap: int) -> None:
    with pytest.raises(ValueError):
        chunk_sentences(_sentences(5), size=size, overlap=overlap)


@pytest.mark.parametrize("size", [0, -1])
def test_non_positive_size_is_rejected(size: int) -> None:
    with pytest.raises(ValueError):
        chunk_sentences(_sentences(5), size=size, overlap=0)


def test_negative_overlap_is_rejected() -> None:
    with pytest.raises(ValueError):
        chunk_sentences(_sentences(5), size=3, overlap=-1)


# -- provenance ------------------------------------------------------------------------------


def test_a_window_takes_the_page_of_its_first_sentence() -> None:
    windows = chunk_sentences(_sentences(4, pages=[1, 1, 2, 2]), size=2, overlap=0)
    assert [w.page for w in windows] == [1, 2]


def test_a_window_straddling_a_page_break_is_filed_under_the_first_page() -> None:
    """The documented loss: only one page is kept, so the tail is mis-attributed."""
    windows = chunk_sentences(_sentences(3, pages=[1, 2, 2]), size=3, overlap=0)
    assert [w.page for w in windows] == [1]
    assert windows[0].text == "s0 s1 s2"  # even though s1 and s2 are on page 2


def test_sentences_without_a_page_yield_windows_without_one() -> None:
    """DOCX and HTML have no page boundaries."""
    windows = chunk_sentences(_sentences(3, pages=[None, None, None]), size=2, overlap=0)
    assert all(w.page is None for w in windows)


def test_sentence_range_is_inclusive_at_both_ends() -> None:
    windows = chunk_sentences(_sentences(7), size=3, overlap=1)
    assert [(w.sentence_start, w.sentence_end) for w in windows] == [(0, 2), (2, 4), (4, 6)]


def test_consecutive_ranges_share_the_overlapping_sentences() -> None:
    windows = chunk_sentences(_sentences(6), size=3, overlap=2)
    ends = [w.sentence_end for w in windows]
    starts = [w.sentence_start for w in windows]
    # overlap=2 → each window starts one sentence after the previous one.
    assert starts == [0, 1, 2, 3]
    # And the shared sentences show up as ranges that overlap rather than abut.
    assert ends[0] >= starts[1]


def test_a_short_last_window_reports_its_real_end() -> None:
    windows = chunk_sentences(_sentences(4), size=3, overlap=0)
    assert (windows[-1].sentence_start, windows[-1].sentence_end) == (3, 3)


def test_the_range_uses_the_sentence_index_not_the_list_position() -> None:
    """The range points into ``retrieved_documents.sentences``, so it follows ``index``."""
    sentences = [Sentence(page=1, index=i + 10, text=f"s{i}") for i in range(4)]
    windows = chunk_sentences(sentences, size=2, overlap=0)
    assert [(w.sentence_start, w.sentence_end) for w in windows] == [(10, 11), (12, 13)]


# -- atomic sentences (a rendered table is one unit) -----------------------------------------


def _mixed(atomic_at: set[int], n: int) -> list[Sentence]:
    """``n`` sentences on page 1, atomic at the given 0-based positions."""
    return [
        Sentence(page=1, index=i, text=f"s{i}", atomic=i in atomic_at) for i in range(n)
    ]


def test_an_atomic_sentence_becomes_a_window_of_its_own() -> None:
    windows = chunk_sentences(_mixed({2}, 3), size=5, overlap=1)
    assert [w.text for w in windows] == ["s0 s1", "s2"]


def test_no_window_ever_spans_an_atomic_sentence() -> None:
    windows = chunk_sentences(_mixed({3}, 7), size=3, overlap=1)
    # The run before the table, the table alone, then the run after it.
    assert [w.text for w in windows] == ["s0 s1 s2", "s3", "s4 s5 s6"]


def test_an_atomic_window_reports_a_single_sentence_range() -> None:
    windows = chunk_sentences(_mixed({1}, 3), size=5, overlap=0)
    table = windows[1]
    assert (table.sentence_start, table.sentence_end) == (1, 1)


def test_an_atomic_sentence_first_or_last_leaves_no_empty_window() -> None:
    assert [w.text for w in chunk_sentences(_mixed({0}, 3), size=2, overlap=0)] == [
        "s0",
        "s1 s2",
    ]
    assert [w.text for w in chunk_sentences(_mixed({2}, 3), size=2, overlap=0)] == [
        "s0 s1",
        "s2",
    ]


def test_consecutive_atomic_sentences_each_get_their_own_window() -> None:
    windows = chunk_sentences(_mixed({1, 2}, 4), size=5, overlap=0)
    assert [w.text for w in windows] == ["s0", "s1", "s2", "s3"]


def test_only_atomic_sentences_yields_one_window_each() -> None:
    assert len(chunk_sentences(_mixed({0, 1, 2}, 3), size=5, overlap=1)) == 3


def test_an_atomic_window_keeps_its_own_page() -> None:
    sentences = [
        Sentence(page=1, index=0, text="s0"),
        Sentence(page=4, index=1, text="tabla", atomic=True),
    ]
    assert [w.page for w in chunk_sentences(sentences, size=5, overlap=0)] == [1, 4]


def test_without_atomic_sentences_the_output_is_the_plain_sliding_window() -> None:
    """The regression guard: the flag must change nothing for existing documents."""
    plain = _sentences(7)
    flagged = _mixed(set(), 7)
    assert chunk_sentences(flagged, size=3, overlap=1) == chunk_sentences(
        plain, size=3, overlap=1
    )
