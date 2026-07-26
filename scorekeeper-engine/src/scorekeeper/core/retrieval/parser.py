"""Concrete Parse stage: classify a ``retrieved_context`` cell and split it into refs.

``LlmSourceRefParser`` implements the :class:`~scorekeeper.core.retrieval.protocols.SourceRefParser`
contract — ``detect(cell) -> SourceFormat`` and ``parse(cell) -> list[SourceRef]``. It is the
first concrete stage of the retrieval pipeline (see ``docs/retrieval-pipeline.md``); the later
stages (locate/fetch/extract) remain deferred.

Three input kinds are handled:

* **JSON** — the two documented shapes (``[{"name", "url"}, ...]`` and
  ``[{"index", "url", "name"}, ...]``) are parsed **deterministically**, with no model call.
* **Pipe-labelled** — the ``name (url) | name (url) | ...`` shape is split and parsed
  **deterministically** with a regex, with no model call.
* **Free-form text** — any remaining non-JSON, non-pipe cell (and any JSON/pipe cell that
  fails to map to refs) is handed to a cheap **OpenAI** model to extract the source references.

The OpenAI SDK is imported lazily and the client is built from ``OPENAI_API_KEY`` (with an
optional ``OPENAI_BASE_URL`` override for OpenAI-compatible gateways), so deterministic JSON
parsing needs no credentials and importing this module never requires the SDK. The parser only
assigns ``rank`` (list position); it never fabricates it, and the model is asked only for
``name``/``url``/``index``. Filling document ``content`` is a later stage's job.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel

from scorekeeper.core.retrieval.types import SourceFormat, SourceRef

# The cheapest chat model in the OpenAI allow-list — the parser does bulk, low-stakes
# extraction, so it runs on the least expensive tier.
DEFAULT_MODEL = "gpt-4o-mini"

# A pipe-separated segment shaped like ``name (url)`` — the heuristic that flags PIPE_LABELLED.
_PIPE_SEGMENT = re.compile(r"^\s*.*\(\s*\S+\s*\)\s*$")

# Capturing form of the same shape: pull the label and the parenthesized URL out of a segment.
# ``url`` is non-greedy and the whole match is ``$``-anchored, so a URL containing parentheses
# is captured whole (the trailing ``\)`` binds to the segment's final close-paren).
_PIPE_LABELLED_SEGMENT = re.compile(r"^\s*(?P<name>.*?)\s*\(\s*(?P<url>\S+?)\s*\)\s*$")

# Spanish system prompt for the free-form extraction call. The cell is sent verbatim as the
# user message, so JSON braces in it are plain data — never interpolated into the prompt.
_SYSTEM_PROMPT = (
    "Eres un extractor de referencias de fuentes. El mensaje del usuario es una celda de "
    "«contexto recuperado»: una lista ordenada por relevancia de referencias a documentos "
    "(una etiqueta y una URL, a veces con un fragmento «#page=N» y un índice del recuperador). "
    "Extrae cada referencia en el mismo orden en que aparece. Para cada una devuelve su "
    "etiqueta (name), su URL completa incluyendo el fragmento (url) y el índice del recuperador "
    "si existe (index). No inviertas ni reordenes las referencias, no inventes URLs y no "
    "incluyas el texto libre que no sea una referencia."
)


class _ExtractedRef(BaseModel):
    """One source reference as extracted by the model (rank is assigned by the parser)."""

    name: str
    url: str
    index: str | None = None


class _ExtractedRefs(BaseModel):
    """The ordered refs the model extracts from a free-form cell."""

    refs: list[_ExtractedRef] = []


class LlmSourceRefParser:
    """Parse a ``retrieved_context`` cell into ranked ``SourceRef`` objects.

    JSON and pipe-labelled cells are parsed deterministically; only free-form (plaintext) cells
    — and any JSON/pipe cell that fails to map — are extracted with a direct OpenAI call.
    ``client`` may be injected (tests); otherwise a real ``openai.OpenAI`` is built lazily from
    ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` the first time the extraction path is reached, so
    deterministic parsing needs no credentials.
    """

    def __init__(
        self,
        *,
        client: Any | None = None,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._api_key = api_key
        self._base_url = base_url

    # -- SourceRefParser protocol -------------------------------------------------------

    def detect(self, cell: str) -> SourceFormat:
        """Classify how ``cell`` is encoded. Pure — never calls the model."""
        text = cell.strip()
        if not text:
            return SourceFormat.EMPTY
        payload = _try_json(text)
        if isinstance(payload, list):
            items = [item for item in payload if isinstance(item, dict)]
            if items:
                if any("index" in item for item in items):
                    return SourceFormat.JSON_INDEXED
                if any(("url" in item) or ("name" in item) for item in items):
                    return SourceFormat.JSON_NAME_URL
            return SourceFormat.PLAINTEXT
        if payload is None and _looks_pipe_labelled(text):
            return SourceFormat.PIPE_LABELLED
        return SourceFormat.PLAINTEXT

    def parse(self, cell: str) -> list[SourceRef]:
        """Parse ``cell`` into ``SourceRef`` objects in retriever-rank order.

        JSON and pipe-labelled shapes are mapped deterministically; if a deterministic path
        yields nothing (or raises), and for every remaining non-empty plaintext cell, the
        OpenAI extraction path is used instead.
        """
        text = cell.strip()
        source_format = self.detect(text)
        if source_format is SourceFormat.EMPTY:
            return []
        if source_format in (SourceFormat.JSON_NAME_URL, SourceFormat.JSON_INDEXED):
            payload = _try_json(text)
            refs = _refs_from_json(payload) if isinstance(payload, list) else []
            if refs:
                return refs
            # Valid JSON that didn't map to any usable ref — fall back to the model.
        elif source_format is SourceFormat.PIPE_LABELLED:
            refs = _refs_from_pipe(text)
            if refs:
                return refs
            # Pipe-shaped but no segment yielded a usable ref — fall back to the model.
        return self._extract_with_llm(text)

    # -- OpenAI extraction path ---------------------------------------------------------

    def _extract_with_llm(self, cell: str) -> list[SourceRef]:
        """Extract refs from a free-form cell via a direct OpenAI structured-output call."""
        completion = self._get_client().chat.completions.parse(
            model=self._model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": cell},
            ],
            response_format=_ExtractedRefs,
        )
        extracted = completion.choices[0].message.parsed
        if extracted is None:
            return []
        refs: list[SourceRef] = []
        for ref in extracted.refs:
            if not ref.url:
                continue
            # Rank is the position among kept refs (contiguous), matching the JSON path.
            refs.append(SourceRef(name=ref.name, url=ref.url, rank=len(refs), index=ref.index))
        return refs

    def _get_client(self) -> Any:
        if self._client is None:
            import openai  # lazy: only needed when building a real client

            from scorekeeper.config.settings import get_settings

            settings = get_settings()
            self._client = openai.OpenAI(
                api_key=self._api_key or settings.openai_api_key,
                base_url=self._base_url or settings.openai_base_url,
            )
        return self._client


def _try_json(text: str) -> Any | None:
    """Return the parsed JSON value, or ``None`` if ``text`` is not JSON."""
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _looks_pipe_labelled(text: str) -> bool:
    """Whether ``text`` looks like ``name (url) | name (url) | ...``."""
    segments = [segment for segment in text.split("|") if segment.strip()]
    return bool(segments) and all(_PIPE_SEGMENT.match(segment) for segment in segments)


def _refs_from_pipe(text: str) -> list[SourceRef]:
    """Map a ``name (url) | name (url) | ...`` cell to ranked ``SourceRef`` objects.

    Segments are split on ``|``; each is matched against ``_PIPE_LABELLED_SEGMENT`` and the
    parenthesized URL is kept (fragment included). Rank is the position among kept segments
    (contiguous); blank or non-matching segments are skipped. ``index`` is never present in this
    format. Purely deterministic — no model call.
    """
    refs: list[SourceRef] = []
    for segment in text.split("|"):
        if not segment.strip():
            continue
        match = _PIPE_LABELLED_SEGMENT.match(segment)
        if match is None:
            continue
        url = match.group("url")
        if not url:
            continue
        refs.append(SourceRef(name=match.group("name").strip(), url=url, rank=len(refs)))
    return refs


def _refs_from_json(payload: list[Any]) -> list[SourceRef]:
    """Map a JSON list of ``{name, url, index?}`` dicts to ranked ``SourceRef`` objects.

    Rank is the list position (contiguous over the kept items); items without a usable ``url``
    are skipped. ``name`` defaults to the empty string; ``index`` is kept when present.
    """
    refs: list[SourceRef] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url:
            continue
        name = item.get("name")
        index = item.get("index")
        refs.append(
            SourceRef(
                name=name if isinstance(name, str) else "",
                url=url,
                rank=len(refs),
                index=index if isinstance(index, str) else None,
            )
        )
    return refs
