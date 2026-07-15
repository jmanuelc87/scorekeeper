# Metrics catalog

The concrete evaluation metrics that ship in
`src/scorekeeper/metrics/catalog/`. Each entry documents *what* the metric
measures and *how* it is scored; for the taxonomy those metrics are built on
(the `Metric` base classes, scales, the `Judge` seam, registration and
per-scenario selection) see [Evaluation metrics](evaluation-metrics.md), and for
where the scores land see the [Data model](data-model.md).

All rubrics, prompts and justifications are in Spanish.

| Metric (`name`) | Category | Scale | Weight | Higher means | Applies to |
| --- | --- | --- | --- | --- | --- |
| [`hallucination`](#hallucination) | `seguridad` | `Unit()` 0–1 | 1.0 | more hallucination (worse) | `document_retrieval`, `web_search` |

## `hallucination`

**How far a RAG answer departs from its retrieved context.**

`Hallucination` — `src/scorekeeper/metrics/catalog/hallucination.py`

An answer *hallucinates* when it contradicts the context it was supposed to be
grounded on. The metric scores this the way a natural-language-inference (NLI)
model would, but with the LLM-as-a-judge standing in for a trained SNLI
classifier: for each retrieved context document (the **premise**) the judge
classifies the model's answer (the **hypothesis**) as one of three NLI labels —
`entailment`, `neutral`, or `contradiction` — and the metric counts how many
documents the answer contradicts.

```
hallucination = contradicted / len(context_docs)
                # 0 = faithful (no document contradicted)
                # 1 = fully hallucinated (every document contradicted)
```

The raw score is the **hallucination rate** itself, on a `Unit()` (0–1) scale, so
**higher is worse** — a hallucinating turn pulls the weighted turn-score down.
The complementary *faithfulness* reading is simply `1 - hallucination`.

### Inputs

- **Premise(s):** `TurnView.retrieved_context`. It is stored as one free-form
  text blob (`Turn.retrieved_context`), so `split_context_docs()` splits it into
  documents on blank lines — blank-line-separated blocks become separate
  documents, multi-line documents stay intact, and a blob with no blank lines is
  treated as a single document.
- **Hypothesis:** `TurnView.response` — the answer being judged.

### Scoring steps

This is a `MultiStepMetric`: one judge call per document, no single rubric.

1. Split `retrieved_context` into documents.
2. For each document, call `judge.structured(..., schema=NLIJudgment)` — an NLI
   **classification**, so it goes through the judge's `structured()` seam rather
   than `score()`. `NLIJudgment` carries the `NLILabel` and a Spanish
   justification.
3. Count `contradiction` labels; `raw_score = contradicted / len(docs)`.
4. Each document's verdict, plus a summary line, is recorded as a `StepTrace` and
   flattened into the single Spanish `justification`.

**No-context turns.** When `retrieved_context` is empty there is nothing to
contradict, so the metric short-circuits to `raw_score = 0.0` (no hallucination)
with **no judge calls**.

### The NLI prompt

`NLI_PROMPT` frames the judge as an NLI classifier. `{documento}` is the premise
(one document); `{response}` is the hypothesis. It is a **placeholder** — refine
the Spanish wording to taste; the metric's mechanics do not depend on it.

### Scenarios

Registered for the `document_retrieval` and `web_search` use cases, since
hallucination is a grounding concern wherever an answer cites retrieved sources.
To target additional use cases, add them to the decorator and re-sync:

```python
@register(scenarios=["document_retrieval", "web_search", ...])
class Hallucination(MultiStepMetric): ...
```

Then run `sync_selection(session)` once to materialize the new mapping into
`scenario_metrics` (see [Evaluation metrics → Per-scenario
selection](evaluation-metrics.md#per-scenario-selection-in-the-database)).

### Notes

- `judge_model` is `None` on the result: the `Judge.structured()` seam does not
  surface the model name (unlike `score()`), so there is no model to record
  without extending the protocol.
- Because higher = worse, this metric contributes to the rollup in the opposite
  direction from "higher is better" metrics like `correccion`/`utilidad`. That
  mixed direction is deliberate; flip to the faithfulness framing (`1 - rate`) if
  you want every metric to point the same way.

### Tests

`tests/metrics/test_hallucination.py` — document splitting, the
no-contradiction / partial-contradiction / no-context cases, and the
one-`structured`-call-per-document contract. No database and no live LLM: a stub
judge serves scripted `NLIJudgment` values through `structured()`.
