# Scorekeeper

Scorekeeper is a service that benchmarks AI assistant platforms (Copilot, Gemini, Claude) by loading each conversation (user and model interactions) from a `.xlsx` file, storing every turn, and scoring each turn with an LLM-as-a-judge. Per-turn metric scores roll up into scenario- and platform-level averages. All scenarios, prompts, and evaluation outputs are in spanish.

