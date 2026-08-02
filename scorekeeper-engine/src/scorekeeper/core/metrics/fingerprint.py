"""The scoring fingerprint — what a stored ``MetricScore`` was produced from.

Scoring is resumable at *metric* granularity: re-running a scored turn must reuse
a score whose inputs have not moved and re-judge one whose inputs have. That
decision needs a single comparable value per (turn, metric), which is what
:func:`scoring_key` produces — a sha256 over everything the judge saw, persisted
on ``MetricScore.scoring_key`` and compared on the next pass.

The inputs, and why each one is in the hash:

* **metric name + rubric version** — the code-owned half of the rubric. A
  ``MultiStepMetric`` that changes its orchestration bumps ``rubric_version``, and
  without it a code change would silently reuse stale scores.
* **prompt version ids** — the DB-owned half, one per declared ``PromptSlot``.
  Ids rather than a digest of the text because ``PromptVersion.template`` is
  write-once, so the id determines the text, and because ids are what
  ``RunPromptBinding`` already records: the key and the binding tell one story.
* **judge models** — the model each chat step resolves to. A model swap changes
  the score.
* **the whole ``TurnView``** — prompt, response, turn number, history, retrieved
  context (in rank order) and expected output. It *is* the complete set of inputs
  a metric can see, so dumping it wholesale cannot drift as ``TurnView`` grows.
  Note that ``history`` couples turns: editing turn 1's response invalidates every
  later turn of the conversation, which is correct — their judges saw turn 1.

Deliberately *not* hashed: ``Metric.weight`` and ``Metric.scale``, which are
roll-up inputs rather than judge inputs. Not hashed because the ``Judge`` seam
does not expose them: the embedding model (so editing ``openai_embedding_model``
will not invalidate ``answer_relevance`` keys), the judge system prompt, and
``judge_max_tokens``. Changing any of those needs a manual re-score.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from scorekeeper.core.metrics.base import Metric, TurnView


def scoring_key(
    metric: Metric,
    view: TurnView,
    *,
    prompt_versions: Mapping[str, str],
    judge_models: Mapping[str, str],
) -> str:
    """The sha256 fingerprint of one metric's evaluation of one turn.

    ``prompt_versions`` maps a declared slot's slug to its ``PromptVersion.id`` as a
    string; ``judge_models`` maps a ``JudgeStep`` value to the model that step
    resolves to. Both arrive as plain strings so this module stays free of the ORM
    and of every LLM SDK.

    The payload is serialized canonically — ``sort_keys`` for dicts, no whitespace —
    so the same inputs always hash the same. Lists keep their order on purpose:
    ``retrieved_context.documents`` is ordered by retriever rank, which
    ``contextual_precision`` scores against. Returns 64 hex characters, matching the
    ``String(64)`` column.
    """
    payload = {
        "metric": metric.name,
        "rubric_version": metric.rubric_version,
        "prompt_versions": dict(prompt_versions),
        "judge_models": dict(judge_models),
        "turn": view.model_dump(mode="json"),
    }
    canonical = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
