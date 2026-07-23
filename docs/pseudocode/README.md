# Metrics pseudocode

The reference algorithm behind each evaluation metric, one document per metric.
Each page pairs the canonical pseudocode with how Scorekeeper actually implements
it — inputs mapped to `TurnView` fields, the Spanish judge prompts, edge-case
behavior, and an explicit note wherever the implementation diverges from the
reference.

For the prose descriptions and the metric taxonomy these are built on, see
[Metrics catalog](../metrics-catalog.md) and
[Evaluation metrics](../evaluation-metrics.md).

| Metric | Algorithm | Measures | Higher is better |
| --- | --- | --- | --- |
| [Faithfulness (RAGAS)](./faithfulness-ragas.md) | RAGAS | fraction of answer statements **entailed** by the context | yes |
| [Faithfulness (DeepEval)](./faithfulness-deepeval.md) | DeepEval | fraction of answer claims **not contradicted** by the context | yes |
| [Hallucination](./hallucination.md) | NLI | fraction of context documents the answer **contradicts** | no (inverted at rollup) |
| [Answer relevance](./answer-relevance.md) | RAGAS reverse-question | how directly the answer addresses the question | yes |
| [Contextual precision](./contextual-precision.md) | DeepEval Average Precision | whether the retriever **ranks** relevant nodes first | yes |

All rubrics, prompts, and justifications are in Spanish. None of the metrics
import an LLM SDK — they depend only on the `Judge` seam.
