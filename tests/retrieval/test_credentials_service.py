"""Tests for the auth-provider CRUD service."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from scorekeeper.database import AuthProviderConfig, Base
from scorekeeper.retrieval.credentials import service
from scorekeeper.retrieval.credentials.service import (
    ProviderConflictError,
    ProviderValidationError,
)

_KEY = "clave-maestra"


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _payload(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "provider": "sharepoint",
        "host": "cognitactix-my.sharepoint.com",
        "enabled": True,
        "tenant_id": "t",
        "client_id": "c",
        "thumbprint": "th",
        "site_url": "https://cognitactix-my.sharepoint.com/sites/x",
        "settings": None,
        "private_key": "-----BEGIN PRIVATE KEY-----abc",
    }
    data.update(overrides)
    return data


def test_create_returns_safe_view_and_hides_secret(session: Session) -> None:
    view = service.create_provider(_payload(), session=session, encryption_key=_KEY)
    assert view["provider"] == "sharepoint"
    assert view["host"] == "cognitactix-my.sharepoint.com"
    assert view["has_private_key"] is True
    # The read view never carries the key material or salt.
    assert "private_key" not in view
    assert "private_key_encrypted" not in view
    assert "private_key_salt" not in view
    # But the row actually stored the encrypted key (round-trips with the master key).
    row = session.get(AuthProviderConfig, UUID(view["id"]))
    assert row is not None
    assert row.decrypted_private_key(_KEY) == "-----BEGIN PRIVATE KEY-----abc"


def test_create_without_private_key(session: Session) -> None:
    view = service.create_provider(
        _payload(private_key=None), session=session, encryption_key=_KEY
    )
    assert view["has_private_key"] is False


def test_create_unknown_kind_raises(session: Session) -> None:
    with pytest.raises(ProviderValidationError):
        service.create_provider(
            _payload(provider="no-existe"), session=session, encryption_key=_KEY
        )


def test_create_private_key_without_encryption_key_raises(session: Session) -> None:
    with pytest.raises(ProviderValidationError):
        service.create_provider(_payload(), session=session, encryption_key=None)


def test_create_duplicate_provider_host_conflicts(session: Session) -> None:
    service.create_provider(_payload(), session=session, encryption_key=_KEY)
    with pytest.raises(ProviderConflictError):
        service.create_provider(_payload(), session=session, encryption_key=_KEY)


def test_list_filters(session: Session) -> None:
    service.create_provider(_payload(), session=session, encryption_key=_KEY)
    service.create_provider(
        _payload(host="other.sharepoint.com", enabled=False),
        session=session,
        encryption_key=_KEY,
    )
    assert len(service.list_providers(session=session)) == 2
    assert len(service.list_providers(enabled=True, session=session)) == 1
    assert len(service.list_providers(host="other.sharepoint.com", session=session)) == 1
    assert service.list_providers(provider="sharepoint", session=session)[0]["provider"] == "sharepoint"


def test_get_unknown_is_none(session: Session) -> None:
    assert service.get_provider(uuid4(), session=session) is None


def test_update_changes_only_supplied_fields(session: Session) -> None:
    created = service.create_provider(_payload(), session=session, encryption_key=_KEY)
    provider_id = UUID(created["id"])
    updated = service.update_provider(
        provider_id, {"enabled": False, "site_url": "https://new"}, session=session
    )
    assert updated is not None
    assert updated["enabled"] is False
    assert updated["site_url"] == "https://new"
    # Untouched fields keep their values, and the key is not disturbed.
    assert updated["tenant_id"] == "t"
    assert updated["has_private_key"] is True


def test_update_rotates_private_key(session: Session) -> None:
    created = service.create_provider(_payload(), session=session, encryption_key=_KEY)
    provider_id = UUID(created["id"])
    row_before = session.get(AuthProviderConfig, provider_id)
    token_before = row_before.private_key_encrypted

    service.update_provider(
        provider_id, {"private_key": "NEW-PEM"}, session=session, encryption_key=_KEY
    )
    session.refresh(row_before)
    assert row_before.private_key_encrypted != token_before
    assert row_before.decrypted_private_key(_KEY) == "NEW-PEM"


def test_update_clears_private_key_when_falsy(session: Session) -> None:
    created = service.create_provider(_payload(), session=session, encryption_key=_KEY)
    provider_id = UUID(created["id"])
    updated = service.update_provider(
        provider_id, {"private_key": None}, session=session, encryption_key=_KEY
    )
    assert updated is not None
    assert updated["has_private_key"] is False


def test_update_unknown_is_none(session: Session) -> None:
    assert service.update_provider(uuid4(), {"enabled": False}, session=session) is None


def test_delete(session: Session) -> None:
    created = service.create_provider(_payload(), session=session, encryption_key=_KEY)
    provider_id = UUID(created["id"])
    assert service.delete_provider(provider_id, session=session) is True
    assert service.get_provider(provider_id, session=session) is None
    assert service.delete_provider(provider_id, session=session) is False
