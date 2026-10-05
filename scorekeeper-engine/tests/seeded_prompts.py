"""Read the shipped prompt text out of the seeding migrations.

The Spanish templates live only in ``migrations/versions/`` — the prompt catalog itself,
plus every later revision that seeds new text — and the suite never runs Alembic
(``tests/conftest.py`` builds its schema with ``Base.metadata.create_all``). So the suite
reaches the text the only way left: importing those migration modules directly.

That is deliberate, not a workaround. It is what lets the tests exercise the *real*
prompts rather than hand-written stand-ins, and it is what keeps the migration honest —
``tests/core/metrics/test_migration_prompts.py`` asserts every code-declared slot has
exactly one seed satisfying its contract, which is the check the type system can no
longer do now that the text is data.

Importing a migration is safe: ``from alembic import op`` binds a proxy that only fails
when *called*, and ``SEEDED_PROMPTS`` is a module-level constant.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

# ``migrations/`` has no __init__.py and is outside pythonpath, so load each by path.
# In revision order: a later revision may only add slots the earlier one did not seed.
_VERSIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"
_MIGRATIONS = (
    "c1a5e7d3f0b6_add_prompt_catalog.py",
    "d7f2b6c1a840_add_quality_metric_rubrics.py",
    "a6d2f8c4b1e7_add_faithfulness_extract_claims.py",
)


def _load(filename: str) -> Any:
    path = _VERSIONS / filename
    spec = importlib.util.spec_from_file_location(f"_seed_migration_{path.stem}", path)
    if spec is None or spec.loader is None:  # pragma: no cover - packaging accident
        raise RuntimeError(f"No se pudo cargar la migración {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_modules = [_load(filename) for filename in _MIGRATIONS]

#: The loaded seeding migrations, in revision order, for tests about one of them.
MODULES = tuple(_modules)

#: The seed rows, exactly as the migrations will insert them.
SEEDED_PROMPTS: tuple[dict[str, Any], ...] = tuple(
    seed for module in _modules for seed in module.SEEDED_PROMPTS
)


def seed_prompts(bind) -> None:
    """Run every migration's own insert, in revision order, against ``bind``."""
    for module in _modules:
        module._seed_prompts(bind)


#: ``(metric name, slug) -> template``.
BY_SLOT: dict[tuple[str, str], str] = {
    (seed["metric"], seed["slug"]): seed["template"] for seed in SEEDED_PROMPTS
}


def templates_for(metric_name: str) -> dict[str, str]:
    """The ``slug -> template`` map to construct ``metric_name`` with.

    What ``selection.resolve`` builds from the database, without needing one: metric
    unit tests stay database-free while still running the shipped Spanish text.
    """
    return {
        slug: template for (metric, slug), template in BY_SLOT.items() if metric == metric_name
    }


def build(metric_cls, **overrides):
    """Construct ``metric_cls`` with its shipped templates bound, applying ``overrides``.

    ``overrides`` set the per-instance knobs metrics expose as class attributes
    (``n_questions``, ``strict_mode``, ``truths_model``…).
    """
    metric = metric_cls(templates_for(metric_cls.name))
    for key, value in overrides.items():
        setattr(metric, key, value)
    return metric
