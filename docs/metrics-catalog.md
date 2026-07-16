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
| [`answer_relevance`](#answer_relevance) | `rag` | `Unit()` 0–1 | 1.0 | answer sticks closer to the question (better) | `answer_relevance` |
| [`contextual_precision`](#contextual_precision) | `rag` | `Unit()` 0–1 | 1.0 | retriever ranks relevant nodes ahead of irrelevant ones (better) | `contextual_precision` |
| [`faithfulness_ragas`](#faithfulness_ragas) | `rag` | `Unit()` 0–1 | 1.0 | more answer statements entailed by context (better) | `document_retrieval`, `web_search` |
| [`faithfulness_deepeval`](#faithfulness_deepeval) | `rag` | `Unit()` 0–1 | 1.0 | fewer answer claims contradicted by context (better) | `document_retrieval`, `web_search` |
| [`hallucination`](#hallucination) | `seguridad` | `Inverted(Unit())` 0–1 | 1.0 | more hallucination — worse raw score, but `Inverted` normalizes it to higher-is-better faithfulness | `hallucination` |

Every metric here is a `MultiStepMetric` — it orchestrates several `Judge` calls
and flattens their per-step `StepTrace`s into a single Spanish `justification`.
None import an LLM SDK: they depend only on the `Judge` seam.

Every metric declares the `use_case`(s) it applies to via `@register(scenarios=…)`.
`answer_relevance`, `contextual_precision`, and `hallucination` each apply to a use
case named after themselves (`scenarios=["answer_relevance"]`,
`["contextual_precision"]`, `["hallucination"]`), while both `faithfulness_*` metrics
declare `scenarios=["document_retrieval", "web_search"]` since their statement/truth
extraction only makes sense where the answer cites retrieved sources. None of these
catalog metrics belong to the reserved **`default`** set, so a scenario is scored by a
metric only when its comma-separated `use_case` names that metric's use case (see
[Evaluation metrics → Per-scenario
selection](evaluation-metrics.md#per-scenario-selection-in-the-database)).

## `answer_relevance`

**How directly the answer addresses the original question.**

`AnswerRelevance` — `src/scorekeeper/metrics/catalog/answer_relevance.py`

Follows the **reverse-question generation** method (RAGAS): if an answer is on
topic, questions generated *from the answer alone* should look like the question
that was actually asked. The metric generates `n` candidate questions from the
answer, embeds them alongside the original question, and averages the cosine
similarity between the original and each generated question. Higher means the
answer stays closer to what was asked.

```
answer_relevance = mean( cosine(q_original, q_generated_i) for i in 1..n )
                   # clamped to [0, 1]
                   # 1 = the answer is squarely about the question
                   # 0 = the answer is off topic (or no question could be generated)
```

The raw score is already in `[0, 1]`, so the scale is `Unit()` (raw ==
normalized) and **higher is better**.

### Inputs

- **Original question:** `TurnView.prompt`.
- **Answer:** `TurnView.response` — reverse-generation runs against the answer
  *in isolation* (see below).

### Scoring steps

1. **Generate** `n_questions` (default **3**) candidate questions. Each call goes
   through `judge.structured(..., schema=GeneratedQuestion)`. The answer is
   wrapped in its own `TurnView(prompt="", response=turn.response)` so the judge
   never sees the original question and cannot copy it — generation stays
   unbiased. Blank generations are dropped.
2. **Embed** the original question and all generated questions in a single
   `judge.embed([turn.prompt, *questions])` call.
3. **Average** the cosine similarity between the original question and each
   generated question. Cosine lives in `[-1, 1]`; the mean is clamped to `[0, 1]`
   for the `Unit` scale and rollup.

`cosine_similarity()` returns `0.0` for empty or zero-norm vectors, so degenerate
embeddings never raise.

**No-question turns.** If every generation is blank there is nothing to compare,
so the metric short-circuits to `raw_score = 0.0` (no relevance) with **no embed
call**.

### Scenarios

Registered with `@register(scenarios=["answer_relevance"])`, so it materializes under
the `answer_relevance` use case (not the reserved `default` set). To change the use
cases it applies to, edit the decorator and run `sync_selection(session)` once to
re-materialize the mapping (see [Evaluation metrics → Per-scenario
selection](evaluation-metrics.md#per-scenario-selection-in-the-database)).

### Notes

- `n_questions` is a plain class attribute, so it can be overridden per instance
  to trade cost for stability (more questions → steadier mean).
- `judge_model` is read best-effort via `getattr(judge, "model", None)`.

### Tests

`tests/metrics/test_answer_relevance.py` — `cosine_similarity` (including
degenerate inputs), the generate → embed → average happy path, and the
no-question short-circuit. No database and no live LLM: a stub judge scripts the
generated questions and a text→vector embedding table.

## `contextual_precision`

**Does the retriever rank relevant nodes ahead of irrelevant ones?**

`ContextualPrecision` — `src/scorekeeper/metrics/catalog/contextual_precision.py`

A *ranking* metric for the retrieval stage of a RAG turn. Given the ordered list
of retrieved nodes (rank 1 = the node the retriever/re-ranker placed first), it
rewards putting relevant nodes before irrelevant ones. **Order is the whole
point:** the same set of nodes scores higher when the relevant ones come first.
The score is the **Average Precision** of the relevance labels over the ranking.

```
contextual_precision = ( Σ_k precision@k · r_k ) / total_relevant
                        # r_k = 1 if node at rank k is relevant, else 0
                        # precision@k = (relevant nodes seen up to k) / k
                        # 0 = no relevant node retrieved (or none at all)
                        # 1 = every relevant node ranked ahead of every irrelevant one
```

### Inputs

- **Nodes (ranked):** `TurnView.retrieved_context`, stored as one free-form blob
  (`Turn.retrieved_context`). `split_context_docs()` splits it into ordered,
  blank-line-separated nodes (the same node convention `hallucination` uses); the
  block order is the retriever's ranking.
- **Reference:** `TurnView.expected_output` — the ground-truth answer
  (`Turn.expected_output`, added alongside this metric). Nodes are judged against
  this reference, **not** the generator's `response`, which is what makes the
  metric reference-based: ranking is scored honestly even when the generator
  answered badly.

### Scoring steps

This is a `MultiStepMetric`: one judge call per node, no single rubric.

1. Split `retrieved_context` into ordered nodes.
2. For each node, in rank order, call `judge.structured(..., schema=RelevanceVerdict)`
   — a relevance **classification**, so it goes through the `structured()` seam
   rather than `score()`. The node is judged in an isolated
   `TurnView(prompt=…, response="")`, so the judge sees the question and the node
   but never the assistant's actual answer or the other nodes.
3. Turn the verdicts into binary `r_k` in rank order, then compute Average
   Precision: at each rank where a relevant node appears, add
   `precision@k = (relevant seen so far) / k`, then divide by the total relevant
   count.
4. Each node's verdict, plus a summary line, is recorded as a `StepTrace` and
   flattened into the single Spanish `justification`.

**No relevant nodes / no context.** If nothing relevant was retrieved (or
`retrieved_context` is empty), the metric short-circuits to `raw_score = 0.0` —
the empty-context case makes **no judge calls**.

**`strict_mode`.** Off by default. When enabled, the score collapses to a pass/fail:
only a perfect ranking (every relevant node ahead of every irrelevant one → `1.0`)
passes; anything less becomes `0.0`.

### The verdict prompt

`VERDICT_PROMPT` frames the judge as a retrieval evaluator deciding whether a node
is *useful for constructing the expected answer*. Only `{expected_output}` and
`{node}` are interpolated (via `.format()`); the template deliberately contains no
other braces so formatting never trips on stray `{}`. The turn's input question is
appended automatically by the judge.

### Scenarios

Registered with `@register(scenarios=["contextual_precision"])`, so it materializes
under the `contextual_precision` use case (not the reserved `default` set). To change
the use cases it applies to, edit the decorator and run `sync_selection(session)` once
to re-materialize the mapping (see [Evaluation metrics → Per-scenario
selection](evaluation-metrics.md#per-scenario-selection-in-the-database)).

### Notes

- `judge_model` is read best-effort via `getattr(judge, "model", None)`, since the
  `structured()` seam does not itself surface the model name.
- The reference lives on both `TurnView.expected_output` and `Turn.expected_output`
  (Alembic migration `a3f1c2b4d5e6`), mirroring how `retrieved_context` is carried.

### Tests

`tests/metrics/test_contextual_precision.py` — perfect ranking, order sensitivity
(relevant-first `1.0` vs relevant-last `0.5`), interleaved Average Precision, the
no-relevant and no-context short-circuits, both `strict_mode` branches, and a spy
test proving nodes are judged against `expected_output` and never against the
response. No database and no live LLM: a stub judge scripts `RelevanceVerdict`
values through `structured()`.

## `faithfulness_ragas`

**Fraction of the answer's statements that the context supports.**

`FaithfulnessRagas` — `src/scorekeeper/metrics/catalog/faithfulness.py`

The **RAGAS** groundedness algorithm: extract the discrete statements the answer
makes, then verify each *against the context* by positive entailment. The score
is the fraction of statements that can be inferred from the retrieved context.

```
faithfulness_ragas = supported / len(claims)
                     # 1 = every statement is entailed by the context
                     # 0 = none are
```

Raw score is already in `[0, 1]` (`Unit()`), **higher is better**.

### Inputs

- **Answer:** `TurnView.response` — the source of the extracted statements.
- **Retrieved context:** consumed indirectly. The metric never touches
  `retrieved_context`; the judge layer renders it into every prompt (via the
  `{context}` placeholder and an appended "Contexto recuperado" section), so the
  metric stays agnostic to context shape.

### Scoring steps

1. **Extract claims** from the answer via `judge.structured(EXTRACT_CLAIMS,
   schema=Claims)`.
2. If **no claims** were extracted, nothing can be unfaithful → short-circuit to
   `raw_score = 1.0` with no verification calls.
3. **Verify** each claim with `judge.score(VERIFY_RAGAS.format(claim=...),
   scale=Boolean())`: `1` if the claim is entailed by the context, `0` if it is
   not entailed or is contradicted.
4. `raw_score = mean(verdicts)` — on the `Boolean` scale each verdict is `0`/`1`,
   so the mean is exactly `supported / n`.

### The prompts

- `EXTRACT_CLAIMS` turns each sentence of the answer into verifiable, independent
  statements (may reference `{prompt}`/`{response}`).
- `VERIFY_RAGAS` asks whether a single `{claim}` can be inferred from the context.
  It must **not** contain `{prompt}`/`{response}`/`{context}` — the judge appends
  the full turn (including retrieved context) automatically.

### Tests

`tests/metrics/test_faithfulness.py` — the all-supported, partially-supported,
and no-claims cases. No database and no live LLM: a `StubJudge` returns scripted
extractions/verdicts and records call order.

## `faithfulness_deepeval`

**Fraction of the answer's claims the context does not contradict.**

`FaithfulnessDeepeval` — `src/scorekeeper/metrics/catalog/faithfulness.py`

The **DeepEval** groundedness algorithm. It differs from RAGAS in polarity: it
extracts *truths from the context* and *claims from the answer*, then per claim
asks whether the truths **contradict** it. The score is the fraction **not**
contradicted, so an unverifiable claim (one the truths simply don't mention)
*passes* — only a direct contradiction fails. This is more lenient than RAGAS,
which requires positive entailment.

```
faithfulness_deepeval = not_contradicted / len(claims)
                        # 1 = no claim is contradicted by the context
                        # 0 = every claim is contradicted
```

Raw score is already in `[0, 1]` (`Unit()`), **higher is better**.

### Inputs

Same as `faithfulness_ragas`: `TurnView.response` for the claims, and
`retrieved_context` consumed indirectly through the judge's `{context}`
rendering.

### Scoring steps

1. **Extract claims** from the answer (`EXTRACT_CLAIMS` → `Claims`). Claims are
   extracted *first* so the no-claims case short-circuits before paying for
   truths extraction; the reference pseudocode runs the two extractions
   concurrently, so leading with claims is equivalent.
2. If **no claims**, short-circuit to `raw_score = 1.0`.
3. **Extract truths** from the context (`GENERATE_TRUTHS` → `Truths`).
4. **Verify** each claim against the joined truths with
   `judge.score(VERIFY_DEEPEVAL.format(truths=..., claim=...), scale=Boolean())`:
   `0` **only** if the truths directly contradict the claim, else `1` (agrees or
   not mentioned).
5. `raw_score = mean(verdicts) = not_contradicted / n`.

### The prompts

- `EXTRACT_CLAIMS` — shared with the RAGAS variant.
- `GENERATE_TRUTHS` extracts atomic, verifiable facts from the retrieved context
  (references `{context}`).
- `VERIFY_DEEPEVAL` asks whether the `{truths}` contradict the `{claim}`; it too
  must not contain the turn placeholders.

### Tests

`tests/metrics/test_faithfulness.py` — shared with the RAGAS metric; covers the
contradiction, not-mentioned-passes, and no-claims cases with the scripted
`StubJudge`.

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

The raw score is the **hallucination rate** itself (0–1), so **higher is worse**.
Rollup, however, averages *normalized* scores as higher-is-better, so the metric
uses an `Inverted(Unit())` scale: the raw score keeps its intuitive direction
while normalization maps it to the **faithfulness** complement (`1 - rate`), which
is higher-is-better and composes correctly with the other metrics. A hallucinating
turn therefore pulls the weighted turn-score down, as intended.

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

`NLI_PROMPT` frames the judge as a strict NLI classifier. It spells out the three
labels, six judging rules (judge only from the premise, a missing detail is not a
contradiction, a direct conflict in any stated attribute is a contradiction, be
decisive, …), a JSON output shape, and three worked examples. `{documento}` is the
premise (one document); `{response}` is the hypothesis.

### Scenarios

Registered with `@register(scenarios=["hallucination"])`, so it materializes under the
`hallucination` use case rather than the reserved `default` set. To change the use
cases it applies to, edit the decorator and re-sync:

```python
@register(scenarios=["hallucination"])
class Hallucination(MultiStepMetric): ...
```

Then run `sync_selection(session)` once to materialize the new mapping into
`scenario_metrics` (see [Evaluation metrics → Per-scenario
selection](evaluation-metrics.md#per-scenario-selection-in-the-database)).

### Notes

- `judge_model` is `None` on the result: the `Judge.structured()` seam does not
  surface the model name (unlike `score()`), so there is no model to record
  without extending the protocol.
- The raw score is higher-is-worse, but the `Inverted(Unit())` scale normalizes
  it to the faithfulness complement, so at rollup it points the same way as the
  "higher is better" metrics like `correccion`/`utilidad`. The stored `raw_score`
  stays the hallucination rate; only the normalized value is flipped.

### Tests

`tests/metrics/test_hallucination.py` — document splitting, the
no-contradiction / partial-contradiction / no-context cases, and the
one-`structured`-call-per-document contract. No database and no live LLM: a stub
judge serves scripted `NLIJudgment` values through `structured()`.
