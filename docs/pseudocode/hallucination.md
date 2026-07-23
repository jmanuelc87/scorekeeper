# Hallucination — pseudocode

**Groundedness by natural-language inference (NLI).** For each retrieved context
document (the **premise**), classify the model's answer (the **hypothesis**) as
`entailment` / `neutral` / `contradiction`, and count how many documents the
answer contradicts. The score is the **hallucination rate**.

- Metric name: `hallucination`
- Class: `Hallucination` — `src/scorekeeper/metrics/catalog/hallucination.py`
- Category: `seguridad` · Scale: `Inverted(Unit())` 0–1 · Weight: 1.0 · **higher raw is worse**
- Prose reference: [Metrics catalog → `hallucination`](../metrics-catalog.md#hallucination)

## Pseudocode

```
function HALLUCINATION_SCORE(actual_output, context_docs):
    contradicted = 0
    for doc in context_docs:
        # NLI with premise = context doc, hypothesis = the LLM output
        label = NLI_CLASSIFY(premise=doc, hypothesis=actual_output)   # E / N / C
        if label == "contradiction":
            contradicted += 1
    return contradicted / len(context_docs)     # 0 = faithful, 1 = fully hallucinated
```

## Inputs and output

| Symbol | Source | Notes |
| --- | --- | --- |
| `actual_output` | `TurnView.response` | The answer being judged — the NLI **hypothesis**. |
| `context_docs` | `TurnView.retrieved_context` | The NLI **premises**. Stored as one free-form blob (`Turn.retrieved_context`); `split_context_docs()` splits it into documents on blank lines. |
| `NLI_CLASSIFY` | `Judge` seam | The LLM-as-a-judge stands in for a trained SNLI classifier; each call goes through `judge.structured(..., schema=NLIJudgment)`. |
| return value | `MetricResult.raw_score` | The hallucination rate in `[0, 1]`. **Higher is worse.** |

## Step-by-step

This is a `MultiStepMetric`: one judge call per document, no single rubric.

1. **Split** `retrieved_context` into documents with `split_context_docs()` —
   blank-line-separated blocks become separate documents, multi-line documents
   stay intact, and a blob with no blank lines is a single document.
2. **Classify** each document. Call `judge.structured(..., schema=NLIJudgment)` —
   an NLI **classification**, so it goes through the `structured()` seam rather
   than `score()`. `NLIJudgment` carries the `NLILabel` (`entailment` / `neutral`
   / `contradiction`) and a Spanish justification.
3. **Count** `contradiction` labels: `raw_score = contradicted / len(docs)`.
4. Each document's verdict plus a summary line is recorded as a `StepTrace` and
   flattened into the single Spanish `justification`.

## The NLI prompt

`NLI_PROMPT` frames the judge as a strict NLI classifier. It spells out the three
labels, six judging rules (judge only from the premise; a *missing* detail is
**neutral**, not a contradiction; a direct conflict in any stated attribute is a
contradiction; be decisive; …), a JSON output shape, and three worked examples.
`{documento}` is the premise (one document); `{response}` is the hypothesis.

## Score, scale, and direction

```
hallucination = contradicted / len(context_docs)
                # 0 = faithful (no document contradicted)
                # 1 = fully hallucinated (every document contradicted)
```

The raw score is the **hallucination rate**, so **higher is worse** — the
opposite direction from every other metric. Rollup averages *normalized* scores
as higher-is-better, so the metric uses an **`Inverted(Unit())`** scale:

- The stored `raw_score` keeps its intuitive direction (the hallucination rate).
- Normalization maps it to the **faithfulness complement** `1 - rate`, which is
  higher-is-better and composes correctly with metrics like `correccion` /
  `utilidad`.

A hallucinating turn therefore pulls the weighted turn-score **down**, as
intended.

## Edge cases

| Case | Raw score | Normalized | Judge calls |
| --- | --- | --- | --- |
| No context (empty `retrieved_context`) | `0.0` (nothing to contradict) | `1.0` | none |
| No document contradicted | `0.0` | `1.0` | one per document |
| Every document contradicted | `1.0` | `0.0` | one per document |

## Notes

- `judge_model` is `None` on the result: the `Judge.structured()` seam does not
  surface the model name (unlike `score()`), so there is no model to record
  without extending the protocol.
- Only the `contradiction` label counts against the answer. `entailment` and
  `neutral` (a detail simply absent from a premise) both pass — a missing detail
  is not a hallucination.

## Tests

`tests/metrics/test_hallucination.py` — document splitting, the
no-contradiction / partial-contradiction / no-context cases, and the
one-`structured`-call-per-document contract. No database and no live LLM: a stub
judge serves scripted `NLIJudgment` values through `structured()`.
