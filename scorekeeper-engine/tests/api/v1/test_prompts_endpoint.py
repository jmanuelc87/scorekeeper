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


@pytest.fixture
async def verify_id(client: httpx.AsyncClient, registry) -> str:
    """The ``verify`` slot's id, with the catalog materialized.

    Almost every write test needs it, and going through the listing rather than the
    session fixture is also what creates the rows in the first place.
    """
    listed = (await client.get("/api/v1/prompts")).json()
    return next(entry["id"] for entry in listed if entry["slug"] == "verify")


async def _versions(client: httpx.AsyncClient, prompt_id: str) -> list[dict]:
    """The slot's history, newest first, read back through the API."""
    return (await client.get(f"/api/v1/prompts/{prompt_id}")).json()["versions"]


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


# --- creating a draft ----------------------------------------------------------------


async def test_post_version_creates_a_draft(
    client: httpx.AsyncClient, verify_id: str
) -> None:
    response = await client.post(
        f"/api/v1/prompts/{verify_id}/versions",
        json={"template": RUBRICA, "changelog": "Más claro.", "author": "ana"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["version"] == 1  # the fake metrics have no seeded history
    assert body["status"] == "draft"
    assert body["is_active"] is False
    assert body["template"] == RUBRICA
    assert body["changelog"] == "Más claro."
    assert body["created_by"] == "ana"
    # Nothing is published yet, so the audit columns for it stay empty.
    assert body["published_by"] is None
    assert body["published_at"] is None


async def test_post_version_numbers_from_the_history_max(
    client: httpx.AsyncClient, verify_id: str, session: AsyncSession
) -> None:
    """The counter is per prompt and continues past gaps, not past the row count."""
    for version, status in ((1, "published"), (4, "discarded")):
        session.add(
            PromptVersion(
                prompt_id=uuid.UUID(verify_id),
                version=version,
                template=RUBRICA,
                status=status,
            )
        )
    await session.commit()

    body = (
        await client.post(
            f"/api/v1/prompts/{verify_id}/versions", json={"template": RUBRICA}
        )
    ).json()

    assert body["version"] == 5


async def test_post_version_replaces_the_open_draft(
    client: httpx.AsyncClient, verify_id: str
) -> None:
    """``template`` is write-once even as a draft, so a re-save is a new row.

    The displaced draft is discarded rather than rewritten, and keeps its version
    number — the gap in the published history is the honest edit sequence.
    """
    first = (
        await client.post(
            f"/api/v1/prompts/{verify_id}/versions", json={"template": RUBRICA}
        )
    ).json()
    second = (
        await client.post(
            f"/api/v1/prompts/{verify_id}/versions",
            json={"template": RUBRICA + " Corregido."},
        )
    ).json()

    assert first["version"] != second["version"]
    history = await _versions(client, verify_id)
    assert [(row["version"], row["status"]) for row in history] == [
        (second["version"], "draft"),
        (first["version"], "discarded"),
    ]


async def test_post_version_supersedes_the_active_version(
    client: httpx.AsyncClient, verify_id: str, session: AsyncSession
) -> None:
    """``supersedes_id`` records the live text the edit moves away from.

    Checked through the session because it is deliberately not serialized — it is
    provenance for a diff view, not part of the read model.
    """
    live = PromptVersion(
        prompt_id=uuid.UUID(verify_id),
        version=1,
        template=RUBRICA,
        status="published",
        is_active=True,
    )
    session.add(live)
    await session.commit()

    created = (
        await client.post(
            f"/api/v1/prompts/{verify_id}/versions", json={"template": RUBRICA}
        )
    ).json()

    draft = await session.get(PromptVersion, uuid.UUID(created["id"]))
    assert draft.supersedes_id == live.id


async def test_post_version_without_an_active_version_supersedes_nothing(
    client: httpx.AsyncClient, verify_id: str, session: AsyncSession
) -> None:
    created = (
        await client.post(
            f"/api/v1/prompts/{verify_id}/versions", json={"template": RUBRICA}
        )
    ).json()

    draft = await session.get(PromptVersion, uuid.UUID(created["id"]))
    assert draft.supersedes_id is None


async def test_post_version_of_unknown_prompt_is_404(
    client: httpx.AsyncClient, registry
) -> None:
    response = await client.post(
        "/api/v1/prompts/2f4a6c1e-0000-4000-8000-000000000000/versions",
        json={"template": RUBRICA},
    )

    assert response.status_code == 404
    assert "no existe" in response.json()["detail"]


async def test_post_version_of_malformed_id_is_404(
    client: httpx.AsyncClient, registry
) -> None:
    response = await client.post(
        "/api/v1/prompts/no-es-un-uuid/versions", json={"template": RUBRICA}
    )

    assert response.status_code == 404


async def test_post_blank_template_is_422(
    client: httpx.AsyncClient, verify_id: str
) -> None:
    """Whitespace-only slips past ``min_length=1``, so the service refuses it."""
    response = await client.post(
        f"/api/v1/prompts/{verify_id}/versions", json={"template": "   \n  "}
    )

    assert response.status_code == 422
    assert "vacía" in response.json()["detail"]


async def test_post_empty_template_is_422(
    client: httpx.AsyncClient, verify_id: str
) -> None:
    response = await client.post(
        f"/api/v1/prompts/{verify_id}/versions", json={"template": ""}
    )

    assert response.status_code == 422


# --- publishing ----------------------------------------------------------------------


async def _draft(client: httpx.AsyncClient, prompt_id: str, template: str) -> dict:
    response = await client.post(
        f"/api/v1/prompts/{prompt_id}/versions", json={"template": template}
    )
    assert response.status_code == 201
    return response.json()


async def test_publish_activates_the_draft(
    client: httpx.AsyncClient, verify_id: str
) -> None:
    draft = await _draft(client, verify_id, RUBRICA)

    response = await client.post(
        f"/api/v1/prompts/{verify_id}/versions/{draft['id']}/publish",
        json={"author": "ana"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "published"
    assert body["is_active"] is True
    assert body["published_by"] == "ana"
    assert body["published_at"] is not None
    # The listing is what scoring reads through, so it must show the new text.
    listed = (await client.get("/api/v1/prompts")).json()
    verify = next(entry for entry in listed if entry["slug"] == "verify")
    assert verify["active_version"]["id"] == draft["id"]


async def test_publish_deactivates_the_incumbent(
    client: httpx.AsyncClient, verify_id: str
) -> None:
    """Exactly one live version, always.

    This is the regression test for the flush between the deactivate and the activate:
    both are UPDATEs on one table, the unit of work orders them by primary key, and
    ``uq_prompt_version_active`` is immediate — so without the flush this fails
    non-deterministically, depending on how the two random UUIDs happened to sort.
    """
    first = await _draft(client, verify_id, RUBRICA)
    await client.post(
        f"/api/v1/prompts/{verify_id}/versions/{first['id']}/publish", json={}
    )
    second = await _draft(client, verify_id, RUBRICA + " Corregido.")
    await client.post(
        f"/api/v1/prompts/{verify_id}/versions/{second['id']}/publish", json={}
    )

    history = await _versions(client, verify_id)
    assert [row["id"] for row in history if row["is_active"]] == [second["id"]]
    # The superseded version stays published — only rollback can bring it back.
    assert {row["status"] for row in history} == {"published"}


async def test_publish_rejects_a_template_missing_a_required_variable(
    client: httpx.AsyncClient, verify_id: str
) -> None:
    draft = await _draft(client, verify_id, "Solo tengo {claim}.")

    response = await client.post(
        f"/api/v1/prompts/{verify_id}/versions/{draft['id']}/publish", json={}
    )

    assert response.status_code == 422
    assert "truths" in response.json()["detail"]


async def test_publish_rejects_an_unknown_variable(
    client: httpx.AsyncClient, verify_id: str
) -> None:
    draft = await _draft(client, verify_id, "{truths} {claim} {fantasma}")

    response = await client.post(
        f"/api/v1/prompts/{verify_id}/versions/{draft['id']}/publish", json={}
    )

    assert response.status_code == 422
    assert "fantasma" in response.json()["detail"]


async def test_publish_accepts_undeclared_judge_variables(
    client: httpx.AsyncClient, verify_id: str
) -> None:
    """``{context}`` is filled by the judge, so a slot need not declare it."""
    draft = await _draft(client, verify_id, "{truths} {claim} {context}")

    response = await client.post(
        f"/api/v1/prompts/{verify_id}/versions/{draft['id']}/publish", json={}
    )

    assert response.status_code == 200


async def test_publish_rejects_unbalanced_braces(
    client: httpx.AsyncClient, verify_id: str
) -> None:
    """``string.Formatter`` raises a bare English ValueError here — a typo is not a 500."""
    draft = await _draft(client, verify_id, "{truths} {claim} y una llave suelta {")

    response = await client.post(
        f"/api/v1/prompts/{verify_id}/versions/{draft['id']}/publish", json={}
    )

    assert response.status_code == 422
    assert "llaves" in response.json()["detail"]


async def test_publish_an_already_published_version_is_409(
    client: httpx.AsyncClient, verify_id: str
) -> None:
    draft = await _draft(client, verify_id, RUBRICA)
    await client.post(
        f"/api/v1/prompts/{verify_id}/versions/{draft['id']}/publish", json={}
    )

    response = await client.post(
        f"/api/v1/prompts/{verify_id}/versions/{draft['id']}/publish", json={}
    )

    assert response.status_code == 409
    assert "borrador" in response.json()["detail"]


async def test_publish_a_discarded_version_is_409(
    client: httpx.AsyncClient, verify_id: str
) -> None:
    draft = await _draft(client, verify_id, RUBRICA)
    await client.post(
        f"/api/v1/prompts/{verify_id}/versions/{draft['id']}/discard"
    )

    response = await client.post(
        f"/api/v1/prompts/{verify_id}/versions/{draft['id']}/publish", json={}
    )

    assert response.status_code == 409


async def test_publish_unknown_version_is_404(
    client: httpx.AsyncClient, verify_id: str
) -> None:
    response = await client.post(
        f"/api/v1/prompts/{verify_id}/versions/2f4a6c1e-0000-4000-8000-000000000000/publish",
        json={},
    )

    assert response.status_code == 404
    assert "no existe" in response.json()["detail"]


async def test_publish_a_version_of_another_prompt_is_404(
    client: httpx.AsyncClient, registry
) -> None:
    """A version is addressed within its slot, so a cross-slot id is simply unknown."""
    listed = (await client.get("/api/v1/prompts")).json()
    extract_id = next(entry["id"] for entry in listed if entry["slug"] == "extract")
    verify_id = next(entry["id"] for entry in listed if entry["slug"] == "verify")
    draft = await _draft(client, extract_id, "Extrae las verdades de {response}.")

    response = await client.post(
        f"/api/v1/prompts/{verify_id}/versions/{draft['id']}/publish", json={}
    )

    assert response.status_code == 404


# --- activating (rollback) -----------------------------------------------------------


async def test_activate_rolls_back_to_an_older_published_version(
    client: httpx.AsyncClient, verify_id: str
) -> None:
    old = await _draft(client, verify_id, RUBRICA)
    await client.post(f"/api/v1/prompts/{verify_id}/versions/{old['id']}/publish", json={})
    new = await _draft(client, verify_id, RUBRICA + " Corregido.")
    await client.post(f"/api/v1/prompts/{verify_id}/versions/{new['id']}/publish", json={})

    response = await client.post(
        f"/api/v1/prompts/{verify_id}/versions/{old['id']}/activate"
    )

    assert response.status_code == 200
    assert response.json()["is_active"] is True
    history = await _versions(client, verify_id)
    assert [row["id"] for row in history if row["is_active"]] == [old["id"]]


async def test_activate_the_active_version_is_a_noop(
    client: httpx.AsyncClient, verify_id: str
) -> None:
    """Idempotent, so the rollback control can be pressed twice without a 409."""
    draft = await _draft(client, verify_id, RUBRICA)
    await client.post(
        f"/api/v1/prompts/{verify_id}/versions/{draft['id']}/publish", json={}
    )

    response = await client.post(
        f"/api/v1/prompts/{verify_id}/versions/{draft['id']}/activate"
    )

    assert response.status_code == 200
    assert response.json()["is_active"] is True
    history = await _versions(client, verify_id)
    assert len([row for row in history if row["is_active"]]) == 1


async def test_activate_a_draft_is_409(
    client: httpx.AsyncClient, verify_id: str
) -> None:
    draft = await _draft(client, verify_id, RUBRICA)

    response = await client.post(
        f"/api/v1/prompts/{verify_id}/versions/{draft['id']}/activate"
    )

    assert response.status_code == 409
    assert "publicada" in response.json()["detail"]


async def test_activate_a_discarded_version_is_409(
    client: httpx.AsyncClient, verify_id: str
) -> None:
    draft = await _draft(client, verify_id, RUBRICA)
    await client.post(f"/api/v1/prompts/{verify_id}/versions/{draft['id']}/discard")

    response = await client.post(
        f"/api/v1/prompts/{verify_id}/versions/{draft['id']}/activate"
    )

    assert response.status_code == 409


async def test_activate_unknown_version_is_404(
    client: httpx.AsyncClient, verify_id: str
) -> None:
    response = await client.post(
        f"/api/v1/prompts/{verify_id}/versions/2f4a6c1e-0000-4000-8000-000000000000/activate"
    )

    assert response.status_code == 404


# --- discarding ----------------------------------------------------------------------


async def test_discard_a_draft(client: httpx.AsyncClient, verify_id: str) -> None:
    draft = await _draft(client, verify_id, RUBRICA)

    response = await client.post(
        f"/api/v1/prompts/{verify_id}/versions/{draft['id']}/discard"
    )

    assert response.status_code == 200
    assert response.json()["status"] == "discarded"
    # The row survives with its number — nothing is ever deleted.
    history = await _versions(client, verify_id)
    assert [(row["version"], row["status"]) for row in history] == [
        (draft["version"], "discarded")
    ]


async def test_discard_a_published_version_is_409(
    client: httpx.AsyncClient, verify_id: str
) -> None:
    """Including an *inactive* one: a finished run's binding still points at it."""
    old = await _draft(client, verify_id, RUBRICA)
    await client.post(f"/api/v1/prompts/{verify_id}/versions/{old['id']}/publish", json={})
    new = await _draft(client, verify_id, RUBRICA + " Corregido.")
    await client.post(f"/api/v1/prompts/{verify_id}/versions/{new['id']}/publish", json={})

    response = await client.post(
        f"/api/v1/prompts/{verify_id}/versions/{old['id']}/discard"
    )

    assert response.status_code == 409
    assert "borrador" in response.json()["detail"]


async def test_discard_an_already_discarded_version_is_409(
    client: httpx.AsyncClient, verify_id: str
) -> None:
    draft = await _draft(client, verify_id, RUBRICA)
    await client.post(f"/api/v1/prompts/{verify_id}/versions/{draft['id']}/discard")

    response = await client.post(
        f"/api/v1/prompts/{verify_id}/versions/{draft['id']}/discard"
    )

    assert response.status_code == 409


async def test_discard_unknown_version_is_404(
    client: httpx.AsyncClient, verify_id: str
) -> None:
    response = await client.post(
        f"/api/v1/prompts/{verify_id}/versions/2f4a6c1e-0000-4000-8000-000000000000/discard"
    )

    assert response.status_code == 404


# --- the invariant the whole design exists to protect --------------------------------


async def test_a_slot_never_loses_its_active_version(
    client: httpx.AsyncClient, verify_id: str
) -> None:
    """Once a slot has a live version it keeps exactly one, through every transition.

    ``is_active`` is only ever cleared as half of installing a replacement, which is why
    there is no deactivate route: scoring refuses to run for a metric whose slot is
    dark, so a one-click way to get there would be a foot-gun.
    """

    async def live_count() -> int:
        return len([row for row in await _versions(client, verify_id) if row["is_active"]])

    old = await _draft(client, verify_id, RUBRICA)
    await client.post(f"/api/v1/prompts/{verify_id}/versions/{old['id']}/publish", json={})
    assert await live_count() == 1

    new = await _draft(client, verify_id, RUBRICA + " Corregido.")
    assert await live_count() == 1  # an open draft does not disturb the live version

    await client.post(f"/api/v1/prompts/{verify_id}/versions/{new['id']}/publish", json={})
    assert await live_count() == 1

    await client.post(f"/api/v1/prompts/{verify_id}/versions/{old['id']}/activate")
    assert await live_count() == 1

    abandoned = await _draft(client, verify_id, RUBRICA + " Tercero.")
    await client.post(f"/api/v1/prompts/{verify_id}/versions/{abandoned['id']}/discard")
    assert await live_count() == 1
