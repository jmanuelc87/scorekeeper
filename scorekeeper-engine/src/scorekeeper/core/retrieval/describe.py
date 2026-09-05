"""Describe a rendered table with Claude, so the grid arrives with its subject.

A table survives the extract stage as a markdown pipe table emitted as one **atomic**
sentence (see ``extract_unstructured``), which is exactly what a judge later reads as a
chunk. On its own a grid grounds badly: ``| Norte | 10 | 12 |`` says nothing about what the
columns measure, over what period, or what the table is *for* — that context lives in prose
the chunker filed elsewhere. So each table is described once, in Spanish, in no more than 400
characters, and the description is stored **with** the table, as the line above it.

The call runs through the Claude Agent SDK, the same seam
``core.metrics.judges.agent_judge`` drives: the bundled Claude Code CLI authenticates with
the local session, or with a ``CLAUDE_CODE_OAUTH_TOKEN`` where there is none (a container has
no ``~/.claude``). Like that judge, the describer is deliberately *not* tool-using and is
capped at one turn — it reads a table and writes a sentence.

**Best-effort in every direction.** Any failure — no result message, a CLI-reported error, a
timeout, the SDK not installed, output that does not satisfy the schema — logs a warning and
returns ``None``. A table without a description is the status quo; failing a document over a
missing caption is not. That is also why there is no retry: a description is enrichment, and
a backoff loop inside the extract stage would slow every retrieval to rescue a caption.

The SDK is async while the extract stage is synchronous, so each call drives it with
``asyncio.run``. That is safe for the same reason the judge's is: ``ContentExtractor.extract``
already runs in an ``asyncio.to_thread`` worker (``core.retrieval.pipeline``), never on the
event loop.

``claude_agent_sdk`` is imported lazily — only when the describer builds its own client — so
importing this module never requires the SDK and tests can inject a fake.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from scorekeeper.config.settings import get_settings

logger = logging.getLogger(__name__)

# The seam: a rendered markdown table -> its description, or ``None`` when none could be
# produced. Injectable so the extractor's tests never reach the SDK.
TableDescriber = Callable[[str], "str | None"]

# The instruction, verbatim; the table's markdown follows it.
PROMPT = "Crea una descripcion de la siguiente tabla en no mas de 400 caracteres:"

# Haiku 4.5 — the cheap bulk tier, and one call is paid per table. Pinned rather than
# aliased, matching the allow-list in ``metrics.judges.anthropic_judge.KNOWN_MODELS``.
MODEL = "claude-haiku-4-5-20251001"

# The ceiling the prompt asks for, enforced here too: the model is asked, not forced, and an
# overrun would push the grid it describes out of its own chunk.
MAX_CHARS = 400

# Wall-clock ceiling (seconds) for one CLI-backed call. The SDK has no per-request timeout
# of its own, so it is applied around the query. Far tighter than the judge's: a caption is
# not worth holding an extract worker for.
DEFAULT_TIMEOUT_SECONDS = 120.0


class _Description(BaseModel):
    """The structured answer one query returns."""

    descripcion: str


class AgentTableDescriber:
    """Describe a markdown table with Claude through the Agent SDK.

    ``client`` may be injected (tests); otherwise the ``claude_agent_sdk`` module itself is
    used — only its ``query`` and ``ClaudeAgentOptions`` are needed.
    """

    def __init__(
        self, *, client: Any | None = None, timeout: float = DEFAULT_TIMEOUT_SECONDS
    ) -> None:
        self.timeout = timeout
        # Environment handed to the CLI subprocess, exactly as the agent judge builds it:
        # with no token configured the mapping stays empty and the CLI uses whatever
        # credentials the inherited environment already provides.
        token = get_settings().claude_code_oauth_token
        self._env = {"CLAUDE_CODE_OAUTH_TOKEN": token} if token else {}
        if client is None:
            import claude_agent_sdk  # lazy: only needed for a real CLI-backed call

            client = claude_agent_sdk
        self._client: Any = client

    def __call__(self, table: str) -> str | None:
        """``table``'s description, or ``None`` when it could not be produced."""
        try:
            result = self._query(f"{PROMPT}\n\n{table}")
            if result is None:
                raise RuntimeError("la consulta no devolvió ningún resultado")
            if getattr(result, "is_error", False):
                detail = "; ".join(getattr(result, "errors", None) or []) or getattr(
                    result, "subtype", "error"
                )
                raise RuntimeError(detail)
            parsed = _Description.model_validate(
                getattr(result, "structured_output", None)
            )
        except Exception:
            # Deliberately broad: a missing SDK, a CLI crash, a timeout and a schema
            # violation are all the same thing here — the table simply goes undescribed.
            logger.warning("No se pudo describir la tabla; queda sin descripción", exc_info=True)
            return None
        # Collapse the newlines a model may return: the description is one line above a
        # grid, and a stray newline there would read as a table row.
        description = " ".join(parsed.descripcion.split())
        return description[:MAX_CHARS] or None

    def _query(self, prompt: str) -> Any:
        """Run one CLI-backed query synchronously and return its result message."""
        options = self._client.ClaudeAgentOptions(
            model=MODEL,
            # No tools and one turn: this reads a table and writes a sentence. No
            # ``thinking`` either — Haiku 4.5 rejects adaptive thinking with a 400.
            tools=[],
            max_turns=1,
            output_format={
                "type": "json_schema",
                "schema": _Description.model_json_schema(),
            },
            env=self._env,
        )
        return asyncio.run(
            asyncio.wait_for(self._drain(prompt=prompt, options=options), self.timeout)
        )

    async def _drain(self, *, prompt: str, options: Any) -> Any:
        """Run one query and return its terminal result message (``None`` if absent).

        The result message is picked out by duck-typing on ``structured_output`` — only
        that message carries it — so this module never imports the SDK's types.
        """
        result = None
        async for message in self._client.query(prompt=prompt, options=options):
            if hasattr(message, "structured_output"):
                result = message
        return result
