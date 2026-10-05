# Faithfulness (RAGAS) — pseudocode

**Groundedness by positive entailment.** Decompose the answer into discrete
statements, then verify each one *against the retrieved context*. The score is
the fraction of statements that can be inferred from the context.

- Metric name: `faithfulness_ragas`
- Class: `FaithfulnessRagas` — `scorekeeper-engine/src/scorekeeper/core/metrics/catalog/faithfulness.py`
- Category: `RAG` · Scale: `Unit()` 0–1 · Weight: 1.0 · **higher is better**
- Prose reference: [Metrics catalog → `faithfulness_ragas`](../metrics-catalog.md#faithfulness_ragas)

## Pseudocode

```
INPUT:
  q   : question           (string)
  a   : answer/output      (string)
  c   : context = passages (list[string])
  LLM : judge model
OUTPUT:
  F   : faithfulness score in [0, 1]

FUNCTION ragas_faithfulness(q, a, c, LLM):

  # Step 1 — Statement extraction FROM THE ANSWER
  S = LLM.extract_statements(q, a)
      # prompt: "create one or more statements from each sentence in the answer"
      # S = [s_1, ..., s_n]
  IF len(S) == 0: RETURN 1.0            # nothing to verify

  # Step 2 — Verify each statement AGAINST THE CONTEXT (entailment)
  supported = 0
  FOR s_i IN S:
      verdict = LLM.verify(s_i, c)      # "can s_i be inferred from context?" -> Yes/No (+reason)
      IF verdict == YES:                # <-- POSITIVE support required
          supported += 1

  # Step 3 — Score
  F = supported / len(S)
  RETURN F
```

## Inputs and output

| Symbol | Source | Notes |
| --- | --- | --- |
| `q` | `TurnView.prompt` | The user's question for the turn. |
| `a` | `TurnView.response` | The answer being scored — the source of the statements. |
| `c` | retrieved context | Consumed *indirectly*: the metric never reads `retrieved_context`; the judge layer renders it into every prompt via the `{context}` placeholder and an appended "Contexto recuperado" section. |
| `LLM` | `Judge` seam | Never an SDK import — the metric depends only on the `Judge` Protocol. |
| `F` | `MetricResult.raw_score` | Already in `[0, 1]`, so `Unit()` makes raw == normalized. |

## Step-by-step

### Step 1 — Statement extraction from the answer

Break the answer into a list of discrete, independently-checkable statements
`S = [s_1, …, s_n]`. If no statement can be extracted there is nothing to verify,
so the metric **short-circuits to `F = NOT_APPLICABLE`** with **no verification
calls** — see [Not-applicable scores](../evaluation-metrics.md#not-applicable-scores).

### Step 2 — Verify each statement against the context

For every statement `s_i`, ask the judge whether it *can be inferred from the
context*. This is **positive entailment**: a statement counts as supported only
when the context actively backs it up. A statement the context neither supports
nor contradicts does **not** count — silence is not support.

- Prompt (`faithfulness_ragas.verify`): *"¿Puede inferirse la siguiente afirmación a partir
  del contexto recuperado? Asigna 1 si la afirmación se deduce del contexto, o 0
  si no se deduce o lo contradice."*
- Each verdict is on the `Boolean()` scale: `1` (entailed) or `0` (not entailed
  or contradicted).

### Step 3 — Score

```
F = supported / len(S)
  # 1 = every statement is entailed by the context
  # 0 = none are
```

Because each Boolean verdict is `0`/`1`, the mean of the verdicts is exactly
`supported / n`.

## Edge cases

| Case | Result | Judge calls |
| --- | --- | --- |
| No statements extracted from the answer | `F = NOT_APPLICABLE` — excluded from the averages, where the reference returns `1.0` | none |
| All statements entailed | `F = 1.0` | one per statement |
| No statement entailed | `F = 0.0` | one per statement |

## RAGAS vs. DeepEval polarity

RAGAS requires **positive entailment** (a statement must be inferable from the
context to pass). Its sibling `faithfulness_deepeval` instead fails a claim
**only on direct contradiction** — an unverifiable claim *passes* there. RAGAS is
therefore the stricter of the two. See
[Metrics catalog → `faithfulness_deepeval`](../metrics-catalog.md#faithfulness_deepeval).

## Note: implementation vs. reference

The pseudocode above is the canonical RAGAS algorithm, where **Step 1 is an LLM
extraction call** (`LLM.extract_statements`). The Scorekeeper implementation
diverges in one place: the extraction is the `extract_claims` prompt slot run
through `judge.structured()` (atomic, verifiable statements), and Step 2
(per-statement verification) is one `judge.decide()` call per statement. The scoring
(Steps 2–3) and the positive-entailment requirement are otherwise identical to the
pseudocode; the no-statements case also short-circuits, but returns
`NOT_APPLICABLE` (excluded from the rollups) where the reference returns `1.0`.

## Tests

`tests/metrics/test_faithfulness.py` — the all-supported, partially-supported,
and no-statements cases. No database and no live LLM: a `StubJudge` returns
scripted extractions/verdicts and records call order.
