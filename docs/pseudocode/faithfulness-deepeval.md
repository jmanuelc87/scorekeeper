# Faithfulness (DeepEval) — pseudocode

**Groundedness by non-contradiction.** Extract *truths from the context* and
*claims from the answer*, then per claim ask whether the truths **contradict**
it. The score is the fraction of claims **not** contradicted — an unverifiable
claim *passes*; only a direct contradiction fails.

- Metric name: `faithfulness_deepeval`
- Class: `FaithfulnessDeepeval` — `src/scorekeeper/metrics/catalog/faithfulness.py`
- Category: `RAG` · Scale: `Unit()` 0–1 · Weight: 1.0 · **higher is better**
- Prose reference: [Metrics catalog → `faithfulness_deepeval`](../metrics-catalog.md#faithfulness_deepeval)

## Pseudocode

```
INPUT:
  input             : user query (string)     # required param, NOT scored
  actual_output     : generated output (string)
  retrieval_context : passages (list[string])
  LLM               : judge model
  truths_extraction_limit : int | None        # optional cap on # of truths
  include_reason    : bool
OUTPUT:
  score  : faithfulness score in [0, 1]
  reason : natural-language justification (optional)

FUNCTION deepeval_faithfulness(actual_output, retrieval_context, LLM, limit, include_reason):

  # Step 1 — Extract TRUTHS from the CONTEXT
  truths = LLM.generate_truths(retrieval_context, limit)

  # Step 2 — Extract CLAIMS from the OUTPUT   (independent of Step 1 -> run concurrently)
  claims = LLM.generate_claims(actual_output)
  IF len(claims) == 0: RETURN 1.0, "no claims"

  # Step 3 — Per-claim verdict: does it CONTRADICT the truths?
  verdicts = []                          # each verdict in {"yes", "no", "idk"}
  FOR claim IN claims:
      v = LLM.generate_verdict(claim, truths)
          # "no"  ONLY if truths DIRECTLY contradict the claim
          # "idk" if not mentioned / unverifiable
          # "yes" if it agrees
      verdicts.append(v)

  # Step 4 — Score = fraction of claims NOT contradicted
  faithful = COUNT(v IN verdicts WHERE v != "no")   # <-- "yes" AND "idk" both pass
  score = faithful / len(verdicts)

  # Step 5 — Optional natural-language reason
  reason = LLM.summarize_contradictions(verdicts, score) IF include_reason ELSE None
  RETURN score, reason
```

## Inputs and output

| Symbol | Source | Notes |
| --- | --- | --- |
| `input` | `TurnView.prompt` | The user's query. Required context, **not scored**. |
| `actual_output` | `TurnView.response` | The answer being scored — the source of the claims. |
| `retrieval_context` | retrieved context | Consumed *indirectly*: the metric never reads `retrieved_context`; the judge renders it into the truths-extraction prompt via the `{context}` placeholder and an appended "Contexto recuperado" section. |
| `LLM` | `Judge` seam | Never an SDK import — the metric depends only on the `Judge` Protocol. |
| `truths_extraction_limit` | — | Not surfaced as a knob in the implementation. |
| `score` | `MetricResult.raw_score` | Already in `[0, 1]`, so `Unit()` makes raw == normalized. |
| `reason` | `MetricResult.justification` | Always produced — the flattened Spanish `StepTrace`s, not optional. |

## Step-by-step

### Step 1 — Extract truths from the context

Pull atomic, verifiable facts out of the retrieved context — the ground truth the
claims will be checked against.

- Prompt (`GENERATE_TRUTHS`): *"Extrae las verdades o hechos presentes en el
  contexto recuperado. Cada verdad debe ser un enunciado atómico y verificable
  tomado únicamente del contexto."* Returns `Truths(truths=[...], summary="")`.

### Step 2 — Extract claims from the output

Break the answer into a list of independently-checkable claims. If **no claim**
can be extracted there is nothing that could be unfaithful, so the metric
**short-circuits to `score = 1.0`** with no verdict calls.

### Step 3 — Per-claim verdict against the truths

For each claim, ask the judge whether the truths **contradict** it.

- Prompt (`VERIFY_DEEPEVAL`): *"¿Las siguientes verdades contradicen la
  afirmación? Asigna 0 SOLO si las verdades contradicen directamente la
  afirmación. Asigna 1 si la afirmación concuerda con las verdades o si no se
  menciona (no verificable)."*
- The three-way `{yes, no, idk}` verdict collapses onto the `Boolean()` scale:
  `0` = contradicted (`"no"`), `1` = agrees **or** not mentioned (`"yes"`/`"idk"`).

### Step 4 — Score = fraction not contradicted

```
score = not_contradicted / len(claims)
        # 1 = no claim is contradicted by the context
        # 0 = every claim is contradicted
```

Because each Boolean verdict is `0`/`1`, the mean of the verdicts is exactly
`not_contradicted / n`. **Both agreement and "not mentioned" pass** — this is
what makes DeepEval more lenient than RAGAS.

## Edge cases

| Case | Result | Judge calls |
| --- | --- | --- |
| No claims extracted from the answer | `score = 1.0` | none |
| No truths extracted from the context | `score = 0.0` (**fail closed**) | claims split only; no verdicts |
| No claim contradicted | `score = 1.0` | truths + one verdict per claim |
| Every claim contradicted | `score = 0.0` | truths + one verdict per claim |

The **empty-truths** case is an implementation refinement the pseudocode does not
cover: with no truths there is no basis to verify anything. Rather than passing
every claim as "no verificable" (which would silently score a fabricated answer
as perfect `1.0`), the metric **fails closed to `0.0`**.

## RAGAS vs. DeepEval polarity

DeepEval fails a claim **only on direct contradiction**; a claim the truths don't
mention passes. RAGAS instead requires **positive entailment** — a statement must
be inferable from the context to pass. RAGAS is therefore the stricter of the two.
See [Faithfulness (RAGAS)](./faithfulness-ragas.md).

## Note: implementation vs. reference

Three divergences from the canonical pseudocode, none affecting the score's
meaning:

1. **Claims are split deterministically.** `generate_claims` is not an LLM call —
   the implementation uses `split_sentences()` (the `syntok` sentence segmenter),
   *one sentence of the answer is one claim*. Truths extraction (Step 1) is still
   a judge call.
2. **Order, not concurrency.** The pseudocode extracts truths and claims
   concurrently. The implementation extracts **claims first** so the no-claims
   case short-circuits *before* paying for the (LLM-based) truths extraction —
   equivalent in result, cheaper on the empty-answer path.
3. **Reason is always produced.** `include_reason` is not a knob; the flattened
   Spanish `StepTrace`s always populate `MetricResult.justification`.

## Tests

`tests/metrics/test_faithfulness.py` — shared with the RAGAS metric; covers the
contradiction, not-mentioned-passes, and no-claims cases with the scripted
`StubJudge`.
