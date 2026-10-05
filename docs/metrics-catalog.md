# Metrics catalog

The concrete evaluation metrics that ship in
`scorekeeper-engine/src/scorekeeper/core/metrics/catalog/`. Each entry documents *what* the metric
measures and *how* it is scored; for the taxonomy those metrics are built on
(the `Metric` base classes, scales, the `Judge` seam, registration and
per-use-case selection) see [Evaluation metrics](evaluation-metrics.md), and for
where the scores land see the [Data model](data-model.md).

All rubrics, prompts and justifications are in Spanish.

> **The prompt text is not in these modules.** Each metric declares only its prompt
> *slots* — referred to below as `metric.slug`, e.g. `faithfulness_ragas.verify`. The
> text itself lives in `prompt_versions`, seeded by a migration and injected at
> resolution, so the wording described here is the shipped default and may have been
> edited since. `GET /api/v1/prompts` shows what is actually live. See
> [Evaluation metrics → Prompt slots](evaluation-metrics.md#prompt-slots). That holds
> for the whole rubric of a single-rubric metric too: it is just the `rubric` slot.

| Metric (`name`) | Category | Scale | Weight | Higher means |
| --- | --- | --- | --- | --- |
| [`answer_relevance`](#answer_relevance) | `rag` | `Unit()` 0–1 | 1.0 | answer sticks closer to the question (better) |
| [`contextual_precision`](#contextual_precision) | `rag` | `Unit()` 0–1 | 1.0 | retriever ranks relevant nodes ahead of irrelevant ones (better) |
| [`faithfulness_ragas`](#faithfulness_ragas) | `rag` | `Unit()` 0–1 | 1.0 | more answer statements entailed by context (better) |
| [`faithfulness_deepeval`](#faithfulness_deepeval) | `rag` | `Unit()` 0–1 | 1.0 | fewer answer claims contradicted by context (better) |
| [`hallucination`](#hallucination) | `seguridad` | `Inverted(Unit())` 0–1 | 1.0 | more hallucination — worse raw score, but `Inverted` normalizes it to higher-is-better faithfulness |
| [`relevancia`](#relevancia) | `calidad` | `Unit()` | 1.0 | the answer addresses the query more directly (better) |
| [`precision`](#precision) | `calidad` | `Unit()` | 1.0 | fewer factual errors (better) |
| [`completitud`](#completitud) | `calidad` | `Unit()` | 1.0 | more of the question's relevant aspects covered (better) |
| [`claridad`](#claridad) | `calidad` | `Unit()` | 1.0 | easier to understand (better) |
| [`razonamiento_logico`](#razonamiento_logico) | `calidad` | `Unit()` | 1.0 | sounder logic and better-justified conclusions (better) |
| [`contextualizacion`](#contextualizacion) | `calidad` | `Unit()` | 1.0 | the scenario's context is understood and integrated (better) |
| [`accionabilidad`](#accionabilidad) | `calidad` | `Unit()` | 1.0 | more practically applicable and executable (better) |
| [`estructura`](#estructura) | `calidad` | `Unit()` | 1.0 | better organized and formatted (better) |
| [`profundidad_analitica`](#profundidad_analitica) | `calidad` | `Unit()` | 1.0 | deeper analysis of the topic (better) |
| [`coherencia_multiturno`](#coherencia_multiturno) | `calidad` | `Unit()` | 1.0 | more consistent across the conversation's turns (better) |

Which use case scores which metric is not shown here — it is data, not a property of the
metric (see below).

Two shapes are represented. The five `rag`/`seguridad` metrics are `MultiStepMetric`s —
each orchestrates several `Judge` calls and records what every step produced as a
structured `MetricTrace` (`steps` → typed `entries`), persisted on the `metric_traces`
table (1:1 with `MetricScore`). The ten [`calidad` metrics](#calidad-the-ten-rubric-metrics)
are `SingleRubricMetric`s: one rubric, one judge call, a one-step trace. None import an
LLM SDK — they depend only on the `Judge` seam.

A metric does not declare which use cases it applies to — that mapping is user data,
composed through `POST /use-cases` (see [Evaluation metrics → Per-use-case
selection](evaluation-metrics.md#per-use-case-selection-in-the-database)).

A fresh database holds exactly one use case, the reserved **`default`**, which scores
**every metric on this page** — so an upload that names no use case gets the full
evaluation, and a metric added later joins it automatically. Create your own use case
when you want a narrower set:

```http
POST /api/v1/use-cases
{"name": "rag_completo", "metrics": ["faithfulness_ragas", "faithfulness_deepeval"]}
```

then ingest under `use_case: "rag_completo"`. Any combination works — the two
`faithfulness_*` variants are independent metrics, so a set may list one, the other, or
both. `default` is the only set maintained from code; every other one is yours.

## `answer_relevance`

**How directly the answer addresses the original question.**

`AnswerRelevance` — `scorekeeper-engine/src/scorekeeper/core/metrics/catalog/answer_relevance.py`

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
so the metric short-circuits to `raw_score = NOT_APPLICABLE` with **no embed
call**, and rollup leaves the turn out of its averages.

### Use cases

Belongs to no use case until one lists it in `POST /use-cases` — the metric itself
declares nothing (see [Evaluation metrics → Per-use-case
selection](evaluation-metrics.md#per-use-case-selection-in-the-database)).

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

`ContextualPrecision` — `scorekeeper-engine/src/scorekeeper/core/metrics/catalog/contextual_precision.py`

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
2. For each node, in rank order, call `judge.decide(...)` — a yes/no relevance
   **decision**, so it goes through the `decide()` seam rather than `score()`. The node is judged in an isolated
   `TurnView(prompt=…, response="")`, so the judge sees the question and the node
   but never the assistant's actual answer or the other nodes.
3. Turn the verdicts into binary `r_k` in rank order, then compute Average
   Precision: at each rank where a relevant node appears, add
   `precision@k = (relevant seen so far) / k`, then divide by the total relevant
   count.
4. Each node's verdict is a typed `TraceEntry` (`value` = relevant, `metadata.rank`),
   with a summary `TraceStep` for the Average Precision result.

**No relevant nodes / no context.** If nothing relevant was retrieved the metric
short-circuits to `raw_score = 0.0` — a real measurement of a failed ranking. If
`retrieved_context` is empty there is no ranking to measure at all, so it returns
`raw_score = NOT_APPLICABLE` with **no judge calls** and stays out of the averages.

**`strict_mode`.** Off by default. When enabled, the score collapses to a pass/fail:
only a perfect ranking (every relevant node ahead of every irrelevant one → `1.0`)
passes; anything less becomes `0.0`.

### The verdict prompt

`contextual_precision.verdict` frames the judge as a retrieval evaluator deciding whether a node
is *useful for constructing the expected answer*. Only `{expected_output}` and
`{node}` are interpolated (via `.format()`); the template deliberately contains no
other braces so formatting never trips on stray `{}`. The turn's input question is
appended automatically by the judge.

### Use cases

Belongs to no use case until one lists it in `POST /use-cases` — the metric itself
declares nothing (see [Evaluation metrics → Per-use-case
selection](evaluation-metrics.md#per-use-case-selection-in-the-database)).

### Notes

- `judge_model` is the model the decisions report they ran on — TypeSafe's Jev when a
  decision judge is configured (see [Evaluation metrics → The Judge
  seam](evaluation-metrics.md#the-judge-seam)).
- The reference lives on both `TurnView.expected_output` and `Turn.expected_output`
  (Alembic migration `a3f1c2b4d5e6`), mirroring how `retrieved_context` is carried.

### Tests

`tests/metrics/test_contextual_precision.py` — perfect ranking, order sensitivity
(relevant-first `1.0` vs relevant-last `0.5`), interleaved Average Precision, the
no-relevant and no-context short-circuits, both `strict_mode` branches, and a spy
test proving nodes are judged against `expected_output` and never against the
response. No database and no live LLM: a stub judge scripts `JudgeDecision`
values through `decide()`.

## `faithfulness_ragas`

**Fraction of the answer's statements that the context supports.**

`FaithfulnessRagas` — `scorekeeper-engine/src/scorekeeper/core/metrics/catalog/faithfulness.py`

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
  `retrieved_context`; the judge layer renders it into the `{context}` placeholder
  the `verify` template declares, so the metric stays agnostic to context shape.

### Scoring steps

1. **Extract claims** from the answer via
   `judge.structured(self.prompt("extract_claims"), schema=Claims, step=EXTRACT)`.
2. If **no claims** were extracted there is nothing to verify → short-circuit to
   `raw_score = NOT_APPLICABLE` with no verification calls, excluded from the
   averages rather than counted as perfect faithfulness.
3. **Verify** each claim with
   `judge.decide(safe_format(self.prompt("verify"), claim=...))`: *yes* if the claim is
   inferable from the context, *no* if it is not entailed or is contradicted.
4. `raw_score = mean(verdicts)` — each verdict counts `1` for *yes* and `0` for *no*,
   so the mean is exactly `supported / n`.

### The prompts

- `faithfulness_ragas.extract_claims` turns each sentence of the answer into atomic,
  verifiable, independent statements (references `{prompt}`/`{response}`).
- `faithfulness_ragas.verify` asks whether a single `{claim}` can be inferred from the
  `{context}` it interpolates. The judge appends the turn itself (prompt, response,
  history) automatically; the retrieved context is *not* appended — it reaches the judge
  only through that `{context}` placeholder.

### Use cases

A separate metric from the DeepEval variant, so a use case may list either or both.
Add it to a set with `POST /use-cases` (see [Evaluation metrics → Per-use-case
selection](evaluation-metrics.md#per-use-case-selection-in-the-database)).

### Tests

`tests/metrics/test_faithfulness.py` — the all-supported, partially-supported,
and no-claims cases. No database and no live LLM: a `StubJudge` returns scripted
extractions/verdicts and records call order.

## `faithfulness_deepeval`

**Fraction of the answer's claims the context does not contradict.**

`FaithfulnessDeepeval` — `scorekeeper-engine/src/scorekeeper/core/metrics/catalog/faithfulness.py`

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

1. **Extract claims** from the answer (`faithfulness_deepeval.extract_claims` → `Claims`,
   pinned to Sonnet like the truths step). Claims are
   extracted *first* so the no-claims case short-circuits before paying for
   truths extraction; the reference pseudocode runs the two extractions
   concurrently, so leading with claims is equivalent.
2. If **no claims**, short-circuit to `raw_score = NOT_APPLICABLE` (nothing to
   measure, excluded from the averages).
3. **Extract truths** from the context (`faithfulness_deepeval.generate_truths` → `Truths`).
4. **Verify** each claim against the joined truths with
   `judge.decide(safe_format(self.prompt("verify"), truths=..., claim=...))`: the
   template asks whether the claim agrees with the truths or is not mentioned, so a
   *yes* counts `1` and a *no* (directly contradicted) `0`.
5. `raw_score = mean(verdicts) = not_contradicted / n`.

### The prompts

- `faithfulness_deepeval.extract_claims` — same text as the RAGAS variant's, seeded for
  each metric by migration `a6d2f8c4b1e7`.
- `faithfulness_deepeval.generate_truths` extracts atomic, verifiable facts from the retrieved context
  (references `{context}`).
- `faithfulness_deepeval.verify` asks whether the `{claim}` agrees with the `{truths}` or is
  not mentioned in them. It
  deliberately omits `{context}`: the truths already extracted from it are what the claim is
  judged against.

### Use cases

A separate metric from the RAGAS variant. A scenario that wants both groundedness
algorithms is ingested under a use case listing both:

```http
POST /api/v1/use-cases
{"name": "rag_completo", "metrics": ["faithfulness_ragas", "faithfulness_deepeval"]}
```

(see [Evaluation metrics → Per-use-case
selection](evaluation-metrics.md#per-use-case-selection-in-the-database)).

### Tests

`tests/metrics/test_faithfulness.py` — shared with the RAGAS metric; covers the
contradiction, not-mentioned-passes, and no-claims cases with the scripted
`StubJudge`.

## `hallucination`

**How far a RAG answer departs from its retrieved context.**

`Hallucination` — `scorekeeper-engine/src/scorekeeper/core/metrics/catalog/hallucination.py`

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
2. For each document, call `judge.choose(..., options=<the three NLILabel values>)`
   — an NLI label is a **multiple-choice decision**, so it goes through the judge's
   `choose()` seam rather than `score()`. The `JudgeChoice` carries the label and,
   from an LLM judge, a Spanish justification.
3. Count `contradiction` labels; `raw_score = contradicted / len(docs)`.
4. Each document's verdict is a typed `TraceEntry` (`value` = the NLI label,
   `justification` = the Spanish rationale), with a summary `TraceStep` for the rate.

**No-context turns.** When `retrieved_context` is empty there is nothing to
contradict and nothing to measure, so the metric short-circuits to
`raw_score = NOT_APPLICABLE` with **no judge calls**. It no longer earns a free
perfect faithfulness score: rollup drops the turn from its averages instead.

### The NLI prompt

`hallucination.nli` frames the judge as a strict NLI classifier. It spells out the three
labels, six judging rules (judge only from the premise, a missing detail is not a
contradiction, a direct conflict in any stated attribute is a contradiction, be
decisive, …), a JSON output shape, and three worked examples. `{documento}` is the
premise (one document); `{response}` is the hypothesis.

### Use cases

Belongs to no use case until one lists it. Add it when creating that use case:

```http
POST /api/v1/use-cases
{"name": "soporte", "metrics": ["hallucination", "answer_relevance"]}
```

(see [Evaluation metrics → Per-use-case
selection](evaluation-metrics.md#per-use-case-selection-in-the-database)).

### Notes

- `judge_model` is the model the choices report they ran on — TypeSafe's Jev when a
  decision judge is configured.
- The raw score is higher-is-worse, but the `Inverted(Unit())` scale normalizes
  it to the faithfulness complement, so at rollup it points the same way as the
  "higher is better" metrics like `correccion`/`utilidad`. The stored `raw_score`
  stays the hallucination rate; only the normalized value is flipped.

### Tests

`tests/metrics/test_hallucination.py` — document splitting, the
no-contradiction / partial-contradiction / no-context cases, and the
one-`structured`-call-per-document contract. No database and no live LLM: a stub
judge serves scripted `JudgeChoice` values through `choose()`.

## `calidad` — the ten rubric metrics

**Ten facets of answer quality, each scored 0–100 by one judge call against one
Spanish rubric.**

`scorekeeper-engine/src/scorekeeper/core/metrics/catalog/{relevancia,precision,completitud,claridad,razonamiento_logico,contextualizacion,accionabilidad,estructura,profundidad_analitica,coherencia_multiturno}.py`
— one module per metric.

Unlike the `rag`/`seguridad` metrics above, these are `SingleRubricMetric`s: the class is
pure declaration (`name`, `category = MetricCategory.CALIDAD`, `scale = Unit()`,
`weight = 1.0`, one `rubric` prompt slot) and `SingleRubricMetric.evaluate` does the rest.
They measure the answer as a piece of communication, so — unlike the RAG metrics — none of
them require an expected output or any other field beyond the turn itself; each rubric does
interpolate the turn's retrieved context through a `{context}` placeholder, so a grounded
answer is judged against the sources it was given.

```
raw_score      = the judge's 0.0-1.0 verdict, clamped into the scale
normalized     = raw                # Unit is already [0, 1], higher is better
```

The judge is told the range automatically (`judges.base.scale_spec` renders
"Asigna un número entre 0.0 y 1.0" for a `Unit`) and clamps whatever the model
returns back into it, so a model answering `85` or `-3` cannot corrupt a rollup.

### Shared mechanics

- **One call.** `evaluate` makes a single `judge.score(..., step=JudgeStep.SCORE)` call
  with the metric's rubric, on the model that step routes to — the decisive scoring step,
  not the cheap extraction tier.
- **The trace** is one `TraceStep` ("Puntuación") holding one `TraceEntry`: the metric
  name, the numeric `value`, the judge's Spanish `justification`, and the model in
  `metadata`.
- **No short-circuit.** There is no `NOT_APPLICABLE` path — every turn has a prompt and a
  response, which is all these rubrics need, so all ten always produce a score.
- **The rubric is the whole prompt.** Each declares exactly one slot, `<metric>.rubric`,
  with no required variables: the only placeholder the templates interpolate is the judge's
  own `{context}`, because `judges.base` appends the turn — turn number, prior history,
  prompt and response — beneath every instruction it sends. That is also why
  `coherencia_multiturno` works without orchestrating anything: the history it judges
  arrives with the turn.
- **Domain-neutral wording.** The rubrics name no sector. The scenario's own context
  reaches the judge with the turn, so pinning a domain into the rubric text would only
  duplicate it.

### The rubrics

Every template states the facet under evaluation, then five bands — `0.90-1.00`,
`0.70-0.89`, `0.50-0.69`, `0.30-0.49`, `0.00-0.29` — and asks for a score plus a brief
Spanish justification. The
ten are listed [at the end of this section](#relevancia).

The full Spanish text is seeded by migration `d7f2b6c1a840`, which also inserts the ten
`metrics` rows the prompts' foreign key needs — the app has never run `sync_metrics` at
migrate time. Its inserts are conditional on the name and slug, so it is safe to re-run
and on a database whose app already booted. Edit the live text through the prompt API,
not the migration.

### Inputs

- **Query:** `TurnView.prompt`.
- **Answer:** `TurnView.response`.
- **History:** `TurnView.history` — appended by the judge to every call, and what
  `coherencia_multiturno` scores.
- **Retrieved context:** interpolated by each rubric's `{context}` placeholder, and empty
  when the turn has none.

### Use cases

Like every metric, these belong to no use case until one lists them — except the reserved
`default`, which `sync_metrics` links to every registered metric on startup, so all ten
join it with no migration. A use case scoring quality only:

```http
POST /api/v1/use-cases
{"name": "calidad", "metrics": ["relevancia", "precision", "completitud", "claridad",
                                "razonamiento_logico", "contextualizacion", "accionabilidad",
                                "estructura", "profundidad_analitica", "coherencia_multiturno"]}
```

(see [Evaluation metrics → Per-use-case
selection](evaluation-metrics.md#per-use-case-selection-in-the-database)).

### Notes

- Adding ten metrics to `default` multiplies the judge calls a full run makes. A narrower
  use case is the lever if that matters for cost.
- `rubric_version` stays `"v1"` on all ten; the *text* is versioned separately in
  `prompt_versions`, and the active version's id is part of each score's
  [scoring key](evaluation-metrics.md), so publishing a new rubric re-judges the turns
  it applies to rather than reusing stale scores.
- `judge_model` is the model `score()` reports, unlike the `structured()`-based metrics
  above which cannot surface one.

### Tests

`scorekeeper-engine/tests/core/metrics/test_quality_metrics.py` — the declaration of all
ten (category, `Unit()`, exactly one `rubric` slot with no required variables),
scoring a turn with the **shipped** Spanish rubric through a stub judge (80 → `0.8`, one
`JudgeStep.SCORE` call), and that each seeded template really states its 0–100 bands.
`tests/core/metrics/test_migration_prompts.py` additionally holds the new slots to the
seed↔slot contract and covers the migration's conditional insert. No database and no live
LLM.

### `relevancia`

**How directly the answer addresses the user's query.** `0.90-1.00`: addresses it fully, all
of it pertinent. `0.00-0.29`: barely relevant, or does not address the query at all.

### `precision`

**Accuracy of the facts, data and information given.** `0.90-1.00`: fully accurate and
verifiable, no factual errors. `0.00-0.29`: fundamentally incorrect or unverifiable.

### `completitud`

**How much of the question's relevant scope the answer covers.** `0.90-1.00`: exhaustive —
every main and secondary aspect. `0.00-0.29`: very little of the topic covered.

### `claridad`

**Ease of comprehension and quality of the writing.** `0.90-1.00`: extremely clear, well
structured, precise language. `0.00-0.29`: confusing, disorganized, near unreadable.

### `razonamiento_logico`

**Quality of the logic, argument coherence and justification of conclusions.** `0.90-1.00`:
sound reasoning, well-justified arguments, valid conclusions. `0.00-0.29`: fallacious or
logically incoherent.

### `contextualizacion`

**Grasp and effective use of the context given in the query and the scenario.** `0.90-1.00`:
deep understanding, expertly integrated. `0.00-0.29`: ignores or misunderstands the context.

### `accionabilidad`

**Practical, executable usefulness of the information.** `0.90-1.00`: highly actionable, with
clear implementation steps. `0.00-0.29`: not applicable, no practical direction.

### `estructura`

**Organization, formatting and logical presentation of the information.** `0.90-1.00`: clear
sections, headings or lists where appropriate. `0.00-0.29`: practically no organization.

### `profundidad_analitica`

**Depth of analysis and exploration of the topic.** `0.90-1.00`: deep and insightful, explores
multiple dimensions. `0.00-0.29`: minimal or absent analysis.

### `coherencia_multiturno`

**Consistency and coherence across the conversation's turns.** `0.90-1.00`: perfectly
coherent, thematically consistent throughout. `0.00-0.29`: incoherent, contradicts earlier
turns. The prior exchanges it judges arrive in `TurnView.history`, which the judge appends
to the rubric — the metric orchestrates nothing itself.
