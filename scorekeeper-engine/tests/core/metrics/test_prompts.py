"""The code-owned half of the prompt catalog: slot declarations and template validation."""

from __future__ import annotations

import pytest

from scorekeeper.core.metrics.prompts import (
    PromptTemplateError,
    placeholders,
    safe_format,
    validate_template,
)
from scorekeeper.core.metrics.registry import MetricRegistry


# --- placeholders -------------------------------------------------------------


def test_placeholders_finds_named_fields() -> None:
    assert placeholders("Verdades:\n{truths}\nAfirmación: {claim}") == {"truths", "claim"}


def test_placeholders_ignores_escaped_braces() -> None:
    """``{{`` is a literal brace, not a field — the NLI prompt embeds JSON that way."""
    assert placeholders('Devuelve {{"label": "entailment"}} para {documento}') == {"documento"}


def test_placeholders_of_a_template_with_none_is_empty() -> None:
    assert placeholders("Sin variables.") == set()


# --- safe_format --------------------------------------------------------------


def test_safe_format_fills_what_it_is_given() -> None:
    assert safe_format("Afirmación: {claim}", claim="Madrid es capital") == (
        "Afirmación: Madrid es capital"
    )


def test_safe_format_leaves_unknown_placeholders_intact() -> None:
    """The judge fills ``{context}`` later; pre-filling must not raise on it."""
    assert safe_format("{claim} en {context}", claim="X") == "X en {context}"


def test_safe_format_unescapes_doubled_braces() -> None:
    assert safe_format('{{"label": "x"}} {claim}', claim="c") == '{"label": "x"} c'


# --- validate_template --------------------------------------------------------


def test_validate_accepts_exactly_the_required_variables() -> None:
    validate_template("Verdades:\n{truths}\nAfirmación: {claim}", ("truths", "claim"))


def test_validate_accepts_judge_variables_without_declaring_them() -> None:
    """``{prompt}``/``{response}``/``{context}`` are filled from the turn by the judge."""
    validate_template("Contexto: {context}", ())


def test_validate_accepts_a_judge_variable_that_is_also_required() -> None:
    """The hallucination prompt pre-fills ``{response}``; the judge's later pass is a no-op."""
    validate_template("{documento} / {response}", ("documento", "response"))


def test_validate_rejects_a_missing_required_variable() -> None:
    with pytest.raises(PromptTemplateError) as exc:
        validate_template("Afirmación: {claim}", ("truths", "claim"))
    assert "truths" in str(exc.value)


def test_validate_rejects_an_unknown_variable() -> None:
    with pytest.raises(PromptTemplateError) as exc:
        validate_template("{claim} y {fantasma}", ("claim",))
    assert "fantasma" in str(exc.value)


def test_validate_ignores_escaped_braces() -> None:
    """A JSON example in the prompt is not an undeclared variable."""
    validate_template('{{"label": "x"}} {documento}', ("documento",))


# --- the shipped slots --------------------------------------------------------


# The shipped templates are no longer reachable from a slot — they live in the
# prompt-catalog migration. Validating them against these contracts is
# ``test_migration_prompts.py``.


def test_declared_slugs_are_unique_within_a_metric(registered_metrics) -> None:
    """``uq_prompt_metric_slug`` would reject a duplicate at sync time instead."""
    for metric_cls in MetricRegistry.all():
        slugs = [slot.slug for slot in metric_cls.prompts]
        assert len(slugs) == len(set(slugs)), metric_cls.name
