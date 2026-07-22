"""Tests for the Parse stage (``LlmSourceRefParser``)."""

from __future__ import annotations

import json

from scorekeeper.retrieval import LlmSourceRefParser, SourceFormat, SourceRef, SourceRefParser
from scorekeeper.retrieval.parser import DEFAULT_MODEL, _ExtractedRef, _ExtractedRefs


class _FakeMessage:
    def __init__(self, parsed: object) -> None:
        self.parsed = parsed


class _FakeChoice:
    def __init__(self, parsed: object) -> None:
        self.message = _FakeMessage(parsed)


class _FakeCompletion:
    def __init__(self, parsed: object) -> None:
        self.choices = [_FakeChoice(parsed)]


class _FakeCompletions:
    def __init__(self, parsed_queue: list[object]) -> None:
        self._parsed_queue = parsed_queue
        self.calls: list[dict[str, object]] = []

    def parse(self, **kwargs: object) -> _FakeCompletion:
        self.calls.append(kwargs)
        return _FakeCompletion(self._parsed_queue.pop(0))


class _FakeOpenAIClient:
    """Mimics ``openai.OpenAI`` structured-output surface: ``chat.completions.parse``."""

    def __init__(self, parsed_queue: list[object]) -> None:
        self.chat = type("_Chat", (), {"completions": _FakeCompletions(parsed_queue)})()

    @property
    def calls(self) -> list[dict[str, object]]:
        return self.chat.completions.calls


class _ExplodingClient:
    """A client that fails if used — proves the deterministic path never calls OpenAI."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError("OpenAI must not be called for a deterministic JSON cell")


# --- detect -------------------------------------------------------------------


def test_detect_empty() -> None:
    parser = LlmSourceRefParser(client=_ExplodingClient())
    assert parser.detect("") is SourceFormat.EMPTY
    assert parser.detect("   \n  ") is SourceFormat.EMPTY


def test_detect_json_name_url() -> None:
    parser = LlmSourceRefParser(client=_ExplodingClient())
    cell = json.dumps(
        [{"name": "cognos.bmv.com.mx", "url": "https://x/a.pdf"}], ensure_ascii=False
    )
    assert parser.detect(cell) is SourceFormat.JSON_NAME_URL


def test_detect_json_indexed() -> None:
    parser = LlmSourceRefParser(client=_ExplodingClient())
    cell = json.dumps(
        [{"index": "1-abc", "url": "https://x/a.pdf#page=1", "name": "host"}],
        ensure_ascii=False,
    )
    assert parser.detect(cell) is SourceFormat.JSON_INDEXED


def test_detect_pipe_labelled() -> None:
    parser = LlmSourceRefParser(client=_ExplodingClient())
    cell = (
        "eleconomista.com.mx (https://eleconomista.com.mx/a) | "
        "cognitactix-my.sharepoint.com (https://sp/b.pdf#page=3)"
    )
    assert parser.detect(cell) is SourceFormat.PIPE_LABELLED


def test_detect_plaintext() -> None:
    parser = LlmSourceRefParser(client=_ExplodingClient())
    assert parser.detect("Un párrafo de texto libre sin referencias.") is SourceFormat.PLAINTEXT


def test_detect_unrecognized_json_shape_is_plaintext() -> None:
    parser = LlmSourceRefParser(client=_ExplodingClient())
    assert parser.detect(json.dumps({"documents": []})) is SourceFormat.PLAINTEXT


# --- deterministic JSON parse (no OpenAI) -------------------------------------


def test_parse_json_name_url_is_deterministic() -> None:
    parser = LlmSourceRefParser(client=_ExplodingClient())
    cell = json.dumps(
        [
            {"name": "cognos.bmv.com.mx", "url": "https://x/a.pdf"},
            {"name": "eleconomista.com.mx", "url": "https://x/b.html"},
        ],
        ensure_ascii=False,
    )
    assert parser.parse(cell) == [
        SourceRef(name="cognos.bmv.com.mx", url="https://x/a.pdf", rank=0),
        SourceRef(name="eleconomista.com.mx", url="https://x/b.html", rank=1),
    ]


def test_parse_json_indexed_preserves_index_and_fragment() -> None:
    parser = LlmSourceRefParser(client=_ExplodingClient())
    cell = json.dumps(
        [
            {"index": "1-abc", "url": "https://x/a.pdf#page=3", "name": "host"},
            {"index": "2-def", "url": "https://x/b.pdf#page=7", "name": "host2"},
        ],
        ensure_ascii=False,
    )
    assert parser.parse(cell) == [
        SourceRef(name="host", url="https://x/a.pdf#page=3", rank=0, index="1-abc"),
        SourceRef(name="host2", url="https://x/b.pdf#page=7", rank=1, index="2-def"),
    ]


def test_parse_json_skips_items_without_url() -> None:
    parser = LlmSourceRefParser(client=_ExplodingClient())
    cell = json.dumps(
        [
            {"name": "no url here"},
            {"name": "host", "url": "https://x/a.pdf"},
        ],
        ensure_ascii=False,
    )
    # Ranks stay contiguous over the kept items.
    assert parser.parse(cell) == [SourceRef(name="host", url="https://x/a.pdf", rank=0)]


def test_parse_empty_returns_empty_list() -> None:
    parser = LlmSourceRefParser(client=_ExplodingClient())
    assert parser.parse("   ") == []


# --- OpenAI free-form path ----------------------------------------------------


def test_parse_plaintext_uses_openai() -> None:
    client = _FakeOpenAIClient(
        [
            _ExtractedRefs(
                refs=[
                    _ExtractedRef(name="host", url="https://x/a.pdf#page=3", index="1-abc"),
                    _ExtractedRef(name="host2", url="https://x/b.html"),
                ]
            )
        ]
    )
    parser = LlmSourceRefParser(client=client)
    cell = "host: https://x/a.pdf#page=3, luego https://x/b.html"
    result = parser.parse(cell)
    assert result == [
        SourceRef(name="host", url="https://x/a.pdf#page=3", rank=0, index="1-abc"),
        SourceRef(name="host2", url="https://x/b.html", rank=1),
    ]
    # The cheapest model was used, the schema was requested, and the cell rode in verbatim.
    call = client.calls[0]
    assert call["model"] == DEFAULT_MODEL
    assert call["response_format"] is _ExtractedRefs
    assert call["messages"][-1] == {"role": "user", "content": cell}


def test_parse_pipe_labelled_is_deterministic() -> None:
    # Pipe-labelled cells are parsed with a regex — OpenAI must never be reached.
    parser = LlmSourceRefParser(client=_ExplodingClient())
    cell = (
        "eleconomista.com.mx (https://eleconomista.com.mx/a) | "
        "cognitactix-my.sharepoint.com (https://sp/b.pdf#page=3)"
    )
    assert parser.parse(cell) == [
        SourceRef(name="eleconomista.com.mx", url="https://eleconomista.com.mx/a", rank=0),
        SourceRef(name="cognitactix-my.sharepoint.com", url="https://sp/b.pdf#page=3", rank=1),
    ]


def test_parse_pipe_labelled_url_with_parentheses() -> None:
    # A URL containing parentheses is captured whole (the trailing paren binds to segment end).
    parser = LlmSourceRefParser(client=_ExplodingClient())
    assert parser.parse("host (https://x/Reporte(2024).pdf)") == [
        SourceRef(name="host", url="https://x/Reporte(2024).pdf", rank=0)
    ]


def test_parse_openai_skips_ref_without_url() -> None:
    client = _FakeOpenAIClient(
        [
            _ExtractedRefs(
                refs=[
                    _ExtractedRef(name="no url", url=""),
                    _ExtractedRef(name="host", url="https://x/a.pdf"),
                ]
            )
        ]
    )
    parser = LlmSourceRefParser(client=client)
    assert parser.parse("texto libre") == [
        SourceRef(name="host", url="https://x/a.pdf", rank=0)
    ]


def test_parse_openai_refusal_returns_empty() -> None:
    # A refusal / non-parse leaves message.parsed as None.
    client = _FakeOpenAIClient([None])
    parser = LlmSourceRefParser(client=client)
    assert parser.parse("texto libre") == []


def test_parse_json_that_fails_to_map_falls_back_to_openai() -> None:
    # Valid JSON list of dicts, but no item carries a usable url -> OpenAI fallback.
    client = _FakeOpenAIClient(
        [_ExtractedRefs(refs=[_ExtractedRef(name="host", url="https://x/a.pdf")])]
    )
    parser = LlmSourceRefParser(client=client)
    cell = json.dumps([{"name": "a"}, {"name": "b"}], ensure_ascii=False)
    assert parser.parse(cell) == [SourceRef(name="host", url="https://x/a.pdf", rank=0)]
    assert len(client.calls) == 1


# --- protocol conformance -----------------------------------------------------


def test_parser_conforms_to_protocol() -> None:
    assert isinstance(LlmSourceRefParser(), SourceRefParser)
