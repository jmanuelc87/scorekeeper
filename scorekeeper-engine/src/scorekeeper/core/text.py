"""Deterministic text segmentation shared across the domain.

``split_sentences`` is the one sentence segmenter the domain uses. Two callers need
it and they sit in different layers, so it lives here rather than in either:

* ``core.metrics.catalog.faithfulness`` decomposes the assistant's answer into
  claims — one sentence is one claim;
* ``core.retrieval.extract`` segments each extracted document page, so a retrieved
  document carries ``(page, index, text)`` sentences a chunker can group later.

``syntok`` is a core dependency (no LLM call, no model download), so this module
imports it at module scope like the metric catalog already did.
"""

from __future__ import annotations

import syntok.segmenter as segmenter


def split_sentences(text: str) -> list[str]:
    """Segment a text blob into sentences with syntok (deterministic, no LLM).

    Each sentence's surface text is reconstructed from its tokens
    (``token.spacing + token.value``), trimmed, and empty fragments are dropped.
    Empty or whitespace-only input yields an empty list.
    """
    sentences: list[str] = []
    for paragraph in segmenter.analyze(text):
        for sentence in paragraph:
            rebuilt = "".join(token.spacing + token.value for token in sentence).strip()
            if rebuilt:
                sentences.append(rebuilt)
    return sentences
