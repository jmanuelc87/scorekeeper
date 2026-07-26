# Answer relevance — pseudocode

**Relevance by reverse-question generation (RAGAS).** If an answer is on topic,
questions generated *from the answer alone* should look like the question that was
actually asked. Generate `n` candidate questions from the answer, embed them
alongside the original question, and average the cosine similarity.

- Metric name: `answer_relevance`
- Class: `AnswerRelevance` — `scorekeeper-engine/src/scorekeeper/core/metrics/catalog/answer_relevance.py`
- Category: `RAG` · Scale: `Unit()` 0–1 · Weight: 1.0 · **higher is better**
- Prose reference: [Metrics catalog → `answer_relevance`](../metrics-catalog.md#answer_relevance)

## Pseudocode

```
function AnswerRelevance(q, answer, n):
    # q       : original user question
    # answer  : generated answer a_s(q) to evaluate
    # n       : number of questions to reverse-generate

    # Step 1: use the LLM to generate n candidate questions
    #         that the given answer could be answering
    generated_questions = []
    for i in 1..n:
        prompt = "Generate a question for the given answer.\n" +
                 "answer: " + answer
        q_i = LLM(prompt)
        generated_questions.append(q_i)

    # Step 2: embed the original question and all generated ones
    #         (paper uses text-embedding-ada-002)
    e_q = embed(q)
    embeddings = [ embed(q_i) for q_i in generated_questions ]

    # Step 3: average cosine similarity between original
    #         question and each reverse-generated question
    total = 0
    for e_qi in embeddings:
        total = total + cosine_similarity(e_q, e_qi)

    AR = total / n
    return AR      # higher = answer more directly addresses q


function cosine_similarity(a, b):
    return dot(a, b) / (norm(a) * norm(b))
```

## Inputs and output

| Symbol | Source | Notes |
| --- | --- | --- |
| `q` | `TurnView.prompt` | The original user question. |
| `answer` | `TurnView.response` | The answer being evaluated. Reverse-generation runs against it **in isolation** (see Step 1). |
| `n` | `n_questions` (default **3**) | A plain class attribute; override per instance to trade cost for stability. |
| `LLM` / `embed` | `Judge` seam | `judge.structured(..., schema=GeneratedQuestion)` for generation; `judge.embed([...])` for embeddings. Never an SDK import. |
| `AR` | `MetricResult.raw_score` | Already in `[0, 1]` after clamping, so `Unit()` makes raw == normalized. |

## Step-by-step

### Step 1 — Reverse-generate `n` candidate questions

Generate `n_questions` (default **3**) candidate questions from the answer. Each
call goes through `judge.structured(..., schema=GeneratedQuestion)`.

- **Isolation is the point.** The answer is wrapped in its own
  `TurnView(prompt="", response=turn.response)` so the judge never sees the
  original question and cannot copy it — generation stays unbiased.
- Blank generations are dropped.

### Step 2 — Embed the original and generated questions

Embed the original question and all generated questions in a **single**
`judge.embed([turn.prompt, *questions])` call. (The RAGAS paper uses
`text-embedding-ada-002`; the concrete embedding model is whatever the judge
provides.)

### Step 3 — Average cosine similarity

```
answer_relevance = mean( cosine(q_original, q_generated_i) for i in 1..n )
                   # clamped to [0, 1]
                   # 1 = the answer is squarely about the question
                   # 0 = the answer is off topic
```

Cosine lives in `[-1, 1]`; the mean is **clamped to `[0, 1]`** for the `Unit`
scale and rollup. `cosine_similarity()` returns `0.0` for empty or zero-norm
vectors, so degenerate embeddings never raise.

## Edge cases

| Case | Result | Embed call |
| --- | --- | --- |
| Every generation blank (no question could be generated) | `raw_score = 0.0` (no relevance) | **none** — short-circuits |
| On-topic answer | high AR (→ 1.0) | one batched call |
| Off-topic answer | low AR (→ 0.0) | one batched call |

**No-question short-circuit** is an implementation detail the pseudocode does not
spell out: if every generation is blank there is nothing to compare, so the
metric returns `0.0` with **no embed call** (the pseudocode would divide by `n`
over an empty/degenerate set).

## Note: implementation vs. reference

- **One batched embed call.** The pseudocode embeds `q` and each `q_i`
  separately; the implementation embeds them all in a single
  `judge.embed([turn.prompt, *questions])` call, then slices — same result,
  fewer round-trips.
- **Clamping.** The raw mean cosine is clamped to `[0, 1]`; the pseudocode's
  `total / n` can in principle be negative for an off-topic answer.
- `judge_model` is read best-effort via `getattr(judge, "model", None)`.

## Tests

`tests/metrics/test_answer_relevance.py` — `cosine_similarity` (including
degenerate inputs), the generate → embed → average happy path, and the
no-question short-circuit. No database and no live LLM: a stub judge scripts the
generated questions and a text→vector embedding table.
