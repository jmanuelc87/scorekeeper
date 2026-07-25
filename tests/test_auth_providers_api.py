"""HTTP-level tests for the auth-provider CRUD endpoints.

Drives the real FastAPI stack against the shared in-memory SQLite DB (the root conftest
points ``SessionLocal`` at it) and stubs the master encryption key so ``private_key`` can
be stored.

Uses ``httpx.AsyncClient`` over ``ASGITransport`` rather than ``TestClient``: the routes
touch the database, and TestClient would drive the app on its own event loop in a worker
thread — a different loop from the one the aiosqlite engine was opened on.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport

from scorekeeper.api import app
from scorekeeper.core.retrieval.credentials import service


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setattr(
        service, "get_settings", lambda: SimpleNamespace(auth_encryption_key="test-key")
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "provider": "sharepoint",
        "host": "cognitactix-my.sharepoint.com",
        "tenant_id": "t",
        "client_id": "c",
        "thumbprint": "th",
        "site_url": "https://cognitactix-my.sharepoint.com/sites/x",
        "private_key": "-----BEGIN PRIVATE KEY-----abc",
    }
    body.update(overrides)
    return body


async def test_create_returns_201_without_secret(client: httpx.AsyncClient) -> None:
    resp = await client.post("/auth-providers", json=_body())
    assert resp.status_code == 201
    data = resp.json()
    assert data["provider"] == "sharepoint"
    assert data["has_private_key"] is True
    # The secret is never returned by the API.
    assert "private_key" not in data
    assert "private_key_encrypted" not in data


async def test_full_lifecycle(client: httpx.AsyncClient) -> None:
    created = (await client.post("/auth-providers", json=_body())).json()
    provider_id = created["id"]

    # read
    got = await client.get(f"/auth-providers/{provider_id}")
    assert got.status_code == 200
    assert got.json()["host"] == "cognitactix-my.sharepoint.com"

    # list
    listed = await client.get("/auth-providers")
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    # update (partial)
    patched = await client.patch(f"/auth-providers/{provider_id}", json={"enabled": False})
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False
    assert patched.json()["tenant_id"] == "t"  # untouched

    # delete
    assert (await client.delete(f"/auth-providers/{provider_id}")).status_code == 204
    assert (await client.get(f"/auth-providers/{provider_id}")).status_code == 404


async def test_duplicate_conflicts_409(client: httpx.AsyncClient) -> None:
    assert (await client.post("/auth-providers", json=_body())).status_code == 201
    assert (await client.post("/auth-providers", json=_body())).status_code == 409


async def test_unknown_kind_422(client: httpx.AsyncClient) -> None:
    resp = await client.post("/auth-providers", json=_body(provider="no-existe"))
    assert resp.status_code == 422


async def test_missing_required_field_422(client: httpx.AsyncClient) -> None:
    # `host` is required by the request schema.
    resp = await client.post("/auth-providers", json=_body(host=None))
    assert resp.status_code == 422


async def test_get_unknown_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/auth-providers/00000000-0000-0000-0000-000000000000")).status_code == 404


async def test_patch_unknown_404(client: httpx.AsyncClient) -> None:
    resp = await client.patch(
        "/auth-providers/00000000-0000-0000-0000-000000000000", json={"enabled": True}
    )
    assert resp.status_code == 404


async def test_patch_empty_body_422(client: httpx.AsyncClient) -> None:
    created = (await client.post("/auth-providers", json=_body())).json()
    resp = await client.patch(f"/auth-providers/{created['id']}", json={})
    assert resp.status_code == 422


async def test_delete_unknown_404(client: httpx.AsyncClient) -> None:
    assert (await client.delete("/auth-providers/00000000-0000-0000-0000-000000000000")).status_code == 404


async def test_bad_uuid_422(client: httpx.AsyncClient) -> None:
    # A non-UUID path segment fails FastAPI's path validation.
    assert (await client.get("/auth-providers/not-a-uuid")).status_code == 422
