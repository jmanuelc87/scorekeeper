"""HTTP-level tests for the auth-provider CRUD endpoints.

Drives the real FastAPI stack against a shared in-memory SQLite DB by pointing the service's
``SessionLocal`` at it (a ``StaticPool`` so every session sees the same database), and stubs
the master encryption key so ``private_key`` can be stored.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from scorekeeper.api import app
from scorekeeper.database import Base
from scorekeeper.retrieval.credentials import service


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(service, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(
        service, "get_settings", lambda: SimpleNamespace(auth_encryption_key="test-key")
    )
    with TestClient(app) as client:
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


def test_create_returns_201_without_secret(client: TestClient) -> None:
    resp = client.post("/auth-providers", json=_body())
    assert resp.status_code == 201
    data = resp.json()
    assert data["provider"] == "sharepoint"
    assert data["has_private_key"] is True
    # The secret is never returned by the API.
    assert "private_key" not in data
    assert "private_key_encrypted" not in data


def test_full_lifecycle(client: TestClient) -> None:
    created = client.post("/auth-providers", json=_body()).json()
    provider_id = created["id"]

    # read
    got = client.get(f"/auth-providers/{provider_id}")
    assert got.status_code == 200
    assert got.json()["host"] == "cognitactix-my.sharepoint.com"

    # list
    listed = client.get("/auth-providers")
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    # update (partial)
    patched = client.patch(f"/auth-providers/{provider_id}", json={"enabled": False})
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False
    assert patched.json()["tenant_id"] == "t"  # untouched

    # delete
    assert client.delete(f"/auth-providers/{provider_id}").status_code == 204
    assert client.get(f"/auth-providers/{provider_id}").status_code == 404


def test_duplicate_conflicts_409(client: TestClient) -> None:
    assert client.post("/auth-providers", json=_body()).status_code == 201
    assert client.post("/auth-providers", json=_body()).status_code == 409


def test_unknown_kind_422(client: TestClient) -> None:
    resp = client.post("/auth-providers", json=_body(provider="no-existe"))
    assert resp.status_code == 422


def test_missing_required_field_422(client: TestClient) -> None:
    # `host` is required by the request schema.
    resp = client.post("/auth-providers", json=_body(host=None))
    assert resp.status_code == 422


def test_get_unknown_404(client: TestClient) -> None:
    assert client.get("/auth-providers/00000000-0000-0000-0000-000000000000").status_code == 404


def test_patch_unknown_404(client: TestClient) -> None:
    resp = client.patch(
        "/auth-providers/00000000-0000-0000-0000-000000000000", json={"enabled": True}
    )
    assert resp.status_code == 404


def test_patch_empty_body_422(client: TestClient) -> None:
    created = client.post("/auth-providers", json=_body()).json()
    resp = client.patch(f"/auth-providers/{created['id']}", json={})
    assert resp.status_code == 422


def test_delete_unknown_404(client: TestClient) -> None:
    assert client.delete("/auth-providers/00000000-0000-0000-0000-000000000000").status_code == 404


def test_bad_uuid_422(client: TestClient) -> None:
    # A non-UUID path segment fails FastAPI's path validation.
    assert client.get("/auth-providers/not-a-uuid").status_code == 422
