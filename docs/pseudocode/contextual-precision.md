# Contextual precision — pseudocode

**Ranking quality of the retriever (DeepEval).** Given the ordered list of
retrieved nodes, reward putting relevant nodes *before* irrelevant ones. The score
is the **Average Precision** of the relevance labels over the ranking — the same
set of nodes scores higher when the relevant ones come first.

- Metric name: `contextual_precision`
- Class: `ContextualPrecision` — `src/scorekeeper/core/metrics/catalog/contextual_precision.py`
- Category: `RAG` · Scale: `Unit()` 0–1 · Weight: 1.0 · **higher is better**
- Prose reference: [Metrics catalog → `contextual_precision`](../metrics-catalog.md#contextual_precision)

## Pseudocode

```
function ContextualPrecision(input, expected_output, retrieval_context):

    # retrieval_context is an ORDERED list of nodes (rank 1 = position the
    # retriever/re-ranker put first). Order is the entire point of this metric.
    nodes = retrieval_context

    # ---- STAGE 1: LLM-as-judge relevance labeling ----
    # Judge each node against the EXPECTED output (ground truth), NOT the
    # generator's actual_output. This is what makes the metric reference-based
    # and keeps ranking evaluation honest even when the generator answered badly.
    verdicts = []                      # binary relevance r_k, in original rank order
    for node in nodes:
        prompt  = build_verdict_prompt(input, expected_output, node)
        verdict = LLM(prompt)          # returns {"verdict": "yes"|"no", "reason": ...}
        r_k     = 1 if verdict.verdict == "yes" else 0
        verdicts.append(r_k)

    # ---- STAGE 2: weighted cumulative precision (== Average Precision) ----
    total_relevant = sum(verdicts)     # "Number of Relevant Nodes"

    if total_relevant == 0:
        return 0.0                     # nothing relevant was retrieved

    running_relevant = 0               # relevant nodes seen up to position k
    weighted_sum     = 0.0

    for k in range(1, len(nodes) + 1):        # k is 1-indexed rank
        r_k = verdicts[k - 1]
        running_relevant += r_k               # "Relevant Nodes Up to Position k"

        if r_k == 1:                          # term contributes only at relevant hits
            precision_at_k = running_relevant / k
            weighted_sum  += precision_at_k * r_k   # r_k==1 here; kept for fidelity

    contextual_precision = weighted_sum / total_relevant

    # ---- optional post-processing ----
    if strict_mode:
        contextual_precision = 1.0 if contextual_precision == 1.0 else 0.0

    reason = LLM_explain(verdicts, contextual_precision)   # if include_reason
    return contextual_precision
```

## Inputs and output

| Symbol | Source | Notes |
| --- | --- | --- |
| `input` | `TurnView.prompt` | The user's question. Appended to each verdict prompt automatically by the judge. |
| `expected_output` | `TurnView.expected_output` | The ground-truth answer (`Turn.expected_output`). Nodes are judged against **this reference**, not the generator's `response` — that is what makes the metric reference-based. |
| `retrieval_context` | `TurnView.retrieved_context` | Stored as one free-form blob; `split_context_docs()` splits it into ordered, blank-line-separated nodes. **Block order is the retriever's ranking.** |
| `LLM` | `Judge` seam | `judge.structured(..., schema=RelevanceVerdict)` — a relevance *classification*, so it goes through `structured()` rather than `score()`. |
| return value | `MetricResult.raw_score` | Average Precision in `[0, 1]`, so `Unit()` makes raw == normalized. |

## Step-by-step

This is a `MultiStepMetric`: one judge call per node, no single rubric.

### Stage 1 — Relevance labeling against the reference

Split `retrieved_context` into ordered nodes, then judge each one **in rank
order** with `judge.structured(..., schema=RelevanceVerdict)`.

- The node is judged in an isolated `TurnView(prompt=…, response="")`, so the
  judge sees the question and the node but **never the assistant's actual answer**
  or the other nodes.
- `VERDICT_PROMPT` frames the judge as a retrieval evaluator deciding whether a
  node is *useful for constructing the expected answer*. Only `{expected_output}`
  and `{node}` are interpolated via `.format()`; the template deliberately
  contains no other braces so formatting never trips on stray `{}`.
- Each `yes`/`no` verdict becomes a binary `r_k`.

### Stage 2 — Average Precision over the ranking

```
contextual_precision = ( Σ_k precision@k · r_k ) / total_relevant
                        # r_k        = 1 if node at rank k is relevant, else 0
                        # precision@k = (relevant nodes seen up to k) / k
                        # 0 = no relevant node retrieved (or none at all)
                        # 1 = every relevant node ranked ahead of every irrelevant one
```

At each rank where a relevant node appears, add `precision@k = running_relevant /
k`, then divide the total by `total_relevant`. **Order sensitivity is the whole
point:** the same nodes score higher when relevant ones come first.

Each node's verdict plus a summary line is recorded as a `StepTrace` and flattened
into the single Spanish `justification`.

## Edge cases

| Case | Result | Judge calls |
| --- | --- | --- |
| No relevant node (or empty `retrieved_context`) | `0.0` | none for empty context |
| Relevant nodes ranked first | `1.0` | one per node |
| Relevant nodes ranked last (e.g. `[irrelevant, relevant]`) | `0.5` | one per node |

**`strict_mode`** (off by default): the score collapses to pass/fail — only a
**perfect** ranking (every relevant node ahead of every irrelevant one → `1.0`)
passes; anything less becomes `0.0`.

## Note: implementation vs. reference

- **Reason is always produced.** `include_reason` is not a knob; the flattened
  Spanish `StepTrace`s always populate `MetricResult.justification`.
- `judge_model` is read best-effort via `getattr(judge, "model", None)`, since the
  `structured()` seam does not itself surface the model name.
- The reference lives on both `TurnView.expected_output` and
  `Turn.expected_output` (Alembic migration `a3f1c2b4d5e6`), mirroring how
  `retrieved_context` is carried.

## Tests

`tests/metrics/test_contextual_precision.py` — perfect ranking, order sensitivity
(relevant-first `1.0` vs relevant-last `0.5`), interleaved Average Precision, the
no-relevant and no-context short-circuits, both `strict_mode` branches, and a spy
test proving nodes are judged against `expected_output` and never against the
response. No database and no live LLM: a stub judge scripts `RelevanceVerdict`
values through `structured()`.
