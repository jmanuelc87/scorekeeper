"""HTTP-level tests for ``/prompts``.

Drives the real FastAPI stack against the shared in-memory SQLite DB (the root conftest
points ``SessionLocal`` at it), over ``httpx.AsyncClient`` rather than ``TestClient``:
these routes touch the database, and TestClient would drive the app on a different event
loop than the aiosqlite engine.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import ClassVar

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.core.metrics.base import Metric, MetricResult, TurnView
from scorekeeper.core.metrics.category import MetricCategory
from scorekeeper.core.metrics.prompts import PromptSlot
from scorekeeper.core.metrics.registry import MetricRegistry
from scorekeeper.core.metrics.scale import Unit
from scorekeeper.db.models import Prompt, PromptVersion
from scorekeeper.main import app

RUBRICA = "¿Contradicen las verdades la afirmación?\nVerdades:\n{truths}\nAfirmación: {claim}"


class Utilidad(Metric):
    """A two-slot metric, so the listing exercises ordering within one metric."""

    name: ClassVar[str] = "utilidad"
    category = MetricCategory.RAG
    scale = Unit()
    prompts = (
        PromptSlot(
            slug="verify",
            required_variables=("truths", "claim"),
            description="Veredicto por afirmación.",
        ),
        PromptSlot(slug="extract", description="Extracción de verdades."),
    )

    def evaluate(self, turn: TurnView, judge) -> MetricResult:  # pragma: no cover - unused
        raise NotImplementedError


class SinPrompts(Metric):
    """A metric declaring no slot — it must not appear in the listing."""

    name: ClassVar[str] = "sin_prompts"
    category = MetricCategory.RAG
    scale = Unit()

    def evaluate(self, turn: TurnView, judge) -> MetricResult:  # pragma: no cover - unused
        raise NotImplementedError


@pytest.fixture
def registry():
    """Isolate the registry to the fake metrics above, then restore."""
    saved = MetricRegistry.all()
    MetricRegistry.clear()
    MetricRegistry.add(Utilidad)
    MetricRegistry.add(SinPrompts)
    try:
        yield
    finally:
        MetricRegistry.clear()
        for metric_cls in saved:
            MetricRegistry.add(metric_cls)


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_get_lists_every_slot_with_its_contract(
    client: httpx.AsyncClient, registry
) -> None:
    response = await client.get("/api/v1/prompts")

    assert response.status_code == 200
    body = response.json()
    # Ordered by metric then slug; ``sin_prompts`` declares none so it is absent.
    assert [(entry["metric"], entry["slug"]) for entry in body] == [
        ("utilidad", "extract"),
        ("utilidad", "verify"),
    ]
    verify = body[1]
    assert verify["required_variables"] == ["truths", "claim"]
    assert verify["description"] == "Veredicto por afirmación."


async def test_a_slot_with_no_seeded_version_lists_as_inactive(
    client: httpx.AsyncClient, registry
) -> None:
    """``sync_prompts`` creates the slot but never its text — that is the migration's job.

    These fake metrics are not in the prompt-catalog migration, so they have no version.
    ``active_version: null`` is the signal an operator needs: the slot exists and scoring
    will refuse to run for its metric until something publishes text for it.
    """
    body = (await client.get("/api/v1/prompts")).json()

    assert [entry["active_version"] for entry in body] == [None, None]


async def test_get_surfaces_a_published_active_version(
    client: httpx.AsyncClient, registry, session: AsyncSession
) -> None:
    await client.get("/api/v1/prompts")  # materialize the slots
    prompt = (
        await session.execute(select(Prompt).where(Prompt.slug == "verify"))
    ).scalars().one()
    session.add(
        PromptVersion(
            prompt_id=prompt.id,
            version=3,
            template=RUBRICA,
            status="published",
            is_active=True,
            created_by="system",
        )
    )
    await session.commit()

    body = (await client.get("/api/v1/prompts")).json()

    verify = next(entry for entry in body if entry["slug"] == "verify")
    assert verify["active_version"]["template"] == RUBRICA
    assert verify["active_version"]["version"] == 3
    assert verify["active_version"]["status"] == "published"
    assert verify["active_version"]["is_active"] is True
    assert verify["active_version"]["created_by"] == "system"
    assert verify["active_version"]["changelog"] is None


async def test_get_materializes_the_catalog_on_first_read(
    client: httpx.AsyncClient, registry
) -> None:
    """A slot is visible before any upload — the listing reconciles against the registry."""
    first = await client.get("/api/v1/prompts")
    assert len(first.json()) == 2


async def test_repeated_gets_do_not_duplicate_slots(
    client: httpx.AsyncClient, registry
) -> None:
    await client.get("/api/v1/prompts")

    body = (await client.get("/api/v1/prompts")).json()

    assert len(body) == 2
    ids = {entry["id"] for entry in body}
    assert len(ids) == 2


async def test_get_by_id_returns_the_version_history(
    client: httpx.AsyncClient, registry, session: AsyncSession
) -> None:
    listed = (await client.get("/api/v1/prompts")).json()
    prompt_id = listed[1]["id"]
    # Two versions, out of order, to prove the history comes back newest first and that
    # the gap a discarded draft leaves is preserved.
    for version, status, active in ((1, "published", False), (4, "published", True)):
        session.add(
            PromptVersion(
                prompt_id=uuid.UUID(prompt_id),
                version=version,
                template=RUBRICA,
                status=status,
                is_active=active,
            )
        )
    await session.commit()

    response = await client.get(f"/api/v1/prompts/{prompt_id}")

    assert response.status_code == 200
    body = response.json()
    assert (body["metric"], body["slug"]) == ("utilidad", "verify")
    assert [version["version"] for version in body["versions"]] == [4, 1]
    assert body["versions"][0]["template"] == RUBRICA


async def test_get_by_id_of_a_slot_with_no_versions_is_empty(
    client: httpx.AsyncClient, registry
) -> None:
    listed = (await client.get("/api/v1/prompts")).json()

    body = (await client.get(f"/api/v1/prompts/{listed[0]['id']}")).json()

    assert body["versions"] == []


async def test_get_unknown_id_is_404(client: httpx.AsyncClient, registry) -> None:
    response = await client.get("/api/v1/prompts/2f4a6c1e-0000-4000-8000-000000000000")

    assert response.status_code == 404
    assert "no existe" in response.json()["detail"]


async def test_get_malformed_id_is_404(client: httpx.AsyncClient, registry) -> None:
    """A non-UUID is "unknown", not a validation error — the path param is a plain str."""
    response = await client.get("/api/v1/prompts/no-es-un-uuid")

    assert response.status_code == 404
