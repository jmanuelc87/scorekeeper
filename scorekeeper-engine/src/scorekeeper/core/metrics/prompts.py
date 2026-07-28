"""Prompt slots — the code-owned half of the prompt catalog.

A metric renders one or more *slots*: a named prompt template it hands to the judge.
:class:`PromptSlot` is that declaration — the slug and the placeholder names the metric
itself supplies. That much is code. The *text* is not: it lives in ``prompt_versions``,
seeded by the prompt-catalog migration, and reaches the metric by injection —
``selection.resolve`` reads the active version and hands it to the constructor (see
:meth:`scorekeeper.core.metrics.base.Metric.prompt`).

That split is what keeps this package free of the database: a metric never queries for
its own prompt, it is handed one.

Deliberately free of any dependency on :mod:`scorekeeper.core.metrics.base`, so ``base``
can import ``PromptSlot`` for its ``Metric.prompts`` declaration without a cycle.

Two placeholder audiences share one template, which is what the validation rule below
is about:

* the **metric** fills its ``required_variables`` before the call (``{claim}``,
  ``{node}``, …), via :func:`safe_format`,
* the **judge** fills ``{prompt}``/``{response}``/``{context}`` afterwards, from the
  turn itself (``judges.base._fill_placeholders``).

So a template may reference either set and nothing else.
"""

from __future__ import annotations

import string
from dataclasses import dataclass

# Placeholders the judge substitutes from the turn, after the metric has filled its own.
# A template may reference these without declaring them; nothing else is filled for it.
JUDGE_VARIABLES = frozenset({"prompt", "response", "context"})


class PromptTemplateError(ValueError):
    """A template does not satisfy its slot's contract. Spanish message."""


@dataclass(frozen=True)
class PromptSlot:
    """One prompt template a metric renders, as declared in code.

    Carries no text. The template a slot resolves to is whichever ``PromptVersion`` is
    active for it, injected at construction; this is only the contract that version must
    satisfy.
    """

    slug: str  # slot name within the metric ("verify", "generate_truths")
    # Placeholder names the metric supplies itself; the judge fills the rest.
    required_variables: tuple[str, ...] = ()
    description: str = ""  # Spanish, what the slot is for; shown in the editor


def placeholders(template: str) -> set[str]:
    """The ``{name}`` placeholders in ``template``.

    Parsed with :class:`string.Formatter` rather than a regex so ``{{``/``}}`` escapes
    are correctly *not* placeholders — the hallucination NLI prompt embeds a JSON
    example that way. Positional (``{}``) and indexed (``{0}``) fields are ignored:
    every slot is filled by name.
    """
    names: set[str] = set()
    for _, name, _, _ in string.Formatter().parse(template):
        if name:  # None for literal-only chunks, "" for a positional field
            names.add(name.split(".", 1)[0].split("[", 1)[0])
    return names


class _SafeDict(dict):
    """Format map that leaves unknown ``{placeholders}`` untouched."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def safe_format(template: str, **values: object) -> str:
    """Fill ``values`` into ``template``, leaving every other placeholder intact.

    What a metric uses instead of ``str.format`` to pre-fill its own variables: the
    judge's ``{prompt}``/``{response}``/``{context}`` must survive the call rather than
    raising ``KeyError`` inside the Celery worker mid-run. Mirrors the judge's own
    ``_fill_placeholders``; kept here because ``judges.base`` already imports from
    ``metrics.base``, so importing back the other way would be a cycle.
    """
    return template.format_map(_SafeDict(values))


def validate_template(template: str, required_variables: tuple[str, ...] | list[str]) -> None:
    """Check ``template`` against a slot's contract, raising ``PromptTemplateError``.

    Every required variable must appear (otherwise the metric computes a value the
    prompt never shows the judge), and nothing may appear beyond the required ones plus
    the judge's ``{prompt}``/``{response}``/``{context}`` (otherwise it reaches the model
    as a literal ``{foo}``).

    Note this deliberately permits a judge variable to *also* be required — the
    hallucination prompt pre-fills ``{response}`` per document, and the judge's later
    pass over it is then a no-op.
    """
    required = set(required_variables)
    found = placeholders(template)

    missing = sorted(required - found)
    if missing:
        raise PromptTemplateError(
            "La plantilla no usa la(s) variable(s) requerida(s): " + ", ".join(missing) + "."
        )

    unknown = sorted(found - required - JUDGE_VARIABLES)
    if unknown:
        raise PromptTemplateError(
            "La plantilla usa variable(s) desconocida(s): " + ", ".join(unknown) + ". "
            "Solo se permiten las variables requeridas del prompt y "
            "{prompt}, {response}, {context}."
        )
