"""Tests for the table describer (extract stage)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scorekeeper.core.retrieval import describe as module
from scorekeeper.core.retrieval.describe import MAX_CHARS, MODEL, PROMPT, AgentTableDescriber

_TABLE = "| Año | Ingreso |\n| --- | --- |\n| 2024 | 2.0 |"


class _FakeAgentSdk:
    """Stands in for the ``claude_agent_sdk`` module the describer drives.

    Only two names are used — ``ClaudeAgentOptions`` (a plain option bag) and ``query``
    (an async iterator ending in a result message) — so these tests run without the SDK
    installed. ``structured_output`` is what the describer duck-types the result on; the
    leading message stands in for the chatter the CLI streams before it.
    """

    def __init__(
        self,
        structured_output: object = None,
        *,
        is_error: bool = False,
        errors: list[str] | None = None,
        raises: Exception | None = None,
        result: bool = True,
    ) -> None:
        self._structured_output = structured_output
        self._is_error = is_error
        self._errors = errors
        self._raises = raises
        self._result = result
        self.calls: list[dict] = []

    def ClaudeAgentOptions(self, **kwargs):  # noqa: N802 — mirrors the SDK's class name
        return SimpleNamespace(**kwargs)

    def query(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return self._messages()

    async def _messages(self):
        yield SimpleNamespace(content="pensando…")  # not the result message
        if not self._result:
            return
        yield SimpleNamespace(
            structured_output=self._structured_output,
            is_error=self._is_error,
            errors=self._errors,
            subtype="error_during_execution" if self._is_error else "success",
        )


def _settings(**overrides):
    values = {"claude_code_oauth_token": None}
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture(autouse=True)
def _default_settings(monkeypatch):
    monkeypatch.setattr(module, "get_settings", _settings)


def _describer(client, **settings):
    if settings:
        module.get_settings = lambda: _settings(**settings)  # noqa: E731
    return AgentTableDescriber(client=client)


# -- the happy path -------------------------------------------------------------------------


def test_it_returns_the_models_description() -> None:
    client = _FakeAgentSdk({"descripcion": "Ingresos por año."})
    assert _describer(client)(_TABLE) == "Ingresos por año."


def test_the_prompt_is_the_instruction_followed_by_the_table() -> None:
    client = _FakeAgentSdk({"descripcion": "x"})
    _describer(client)(_TABLE)
    assert client.calls[0]["prompt"] == f"{PROMPT}\n\n{_TABLE}"


def test_it_calls_haiku_without_tools_or_thinking() -> None:
    """Haiku 4.5 rejects adaptive thinking, and a describer has nothing to act on."""
    client = _FakeAgentSdk({"descripcion": "x"})
    _describer(client)(_TABLE)
    options = client.calls[0]["options"]
    assert options.model == MODEL
    assert options.tools == []
    assert options.max_turns == 1
    assert not hasattr(options, "thinking")
    assert options.output_format["type"] == "json_schema"


def test_a_configured_oauth_token_reaches_the_cli_environment() -> None:
    client = _FakeAgentSdk({"descripcion": "x"})
    _describer(client, claude_code_oauth_token="tok")(_TABLE)
    assert client.calls[0]["options"].env == {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}


def test_without_a_token_the_environment_is_left_alone() -> None:
    """An empty token would shadow the credentials the CLI could otherwise use."""
    client = _FakeAgentSdk({"descripcion": "x"})
    _describer(client)(_TABLE)
    assert client.calls[0]["options"].env == {}


# -- shaping the answer ---------------------------------------------------------------------


def test_newlines_are_collapsed_so_the_caption_stays_one_line() -> None:
    client = _FakeAgentSdk({"descripcion": "Ingresos\npor  año."})
    assert _describer(client)(_TABLE) == "Ingresos por año."


def test_an_overlong_description_is_truncated() -> None:
    client = _FakeAgentSdk({"descripcion": "a" * 900})
    assert len(_describer(client)(_TABLE)) == MAX_CHARS


def test_a_blank_description_is_no_description() -> None:
    client = _FakeAgentSdk({"descripcion": "   "})
    assert _describer(client)(_TABLE) is None


# -- best effort ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client",
    [
        pytest.param(_FakeAgentSdk(result=False), id="no result message"),
        pytest.param(
            _FakeAgentSdk({"descripcion": "x"}, is_error=True, errors=["429"]),
            id="the CLI reported an error",
        ),
        pytest.param(_FakeAgentSdk(raises=RuntimeError("boom")), id="the query raised"),
        pytest.param(_FakeAgentSdk({"otra": "cosa"}), id="output off the schema"),
        pytest.param(_FakeAgentSdk(None), id="no structured output"),
    ],
)
def test_a_failure_leaves_the_table_undescribed(client) -> None:
    """Never raises: failing a document over a missing caption is not worth it."""
    assert _describer(client)(_TABLE) is None
