"""Tests for the auth-provider CRUD service."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.db.models import AuthProviderConfig
from scorekeeper.retrieval.credentials import service
from scorekeeper.retrieval.credentials.service import (
    ProviderConflictError,
    ProviderValidationError,
)

_KEY = "clave-maestra"


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


async def test_create_returns_safe_view_and_hides_secret(session: AsyncSession) -> None:
    view = await service.create_provider(_payload(), session=session, encryption_key=_KEY)
    assert view["provider"] == "sharepoint"
    assert view["host"] == "cognitactix-my.sharepoint.com"
    assert view["has_private_key"] is True
    # The read view never carries the key material or salt.
    assert "private_key" not in view
    assert "private_key_encrypted" not in view
    assert "private_key_salt" not in view
    # But the row actually stored the encrypted key (round-trips with the master key).
    row = await session.get(AuthProviderConfig, UUID(view["id"]))
    assert row is not None
    assert row.decrypted_private_key(_KEY) == "-----BEGIN PRIVATE KEY-----abc"


async def test_create_without_private_key(session: AsyncSession) -> None:
    view = await service.create_provider(
        _payload(private_key=None), session=session, encryption_key=_KEY
    )
    assert view["has_private_key"] is False


async def test_create_unknown_kind_raises(session: AsyncSession) -> None:
    with pytest.raises(ProviderValidationError):
        await service.create_provider(
            _payload(provider="no-existe"), session=session, encryption_key=_KEY
        )


async def test_create_private_key_without_encryption_key_raises(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # encryption_key=None makes _apply_private_key fall back to settings; force that
    # fallback empty so the test does not depend on the ambient .env's AUTH_ENCRYPTION_KEY.
    monkeypatch.setattr(
        service, "get_settings", lambda: SimpleNamespace(auth_encryption_key=None)
    )
    with pytest.raises(ProviderValidationError):
        await service.create_provider(_payload(), session=session, encryption_key=None)


async def test_create_duplicate_provider_host_conflicts(session: AsyncSession) -> None:
    await service.create_provider(_payload(), session=session, encryption_key=_KEY)
    with pytest.raises(ProviderConflictError):
        await service.create_provider(_payload(), session=session, encryption_key=_KEY)


async def test_list_filters(session: AsyncSession) -> None:
    await service.create_provider(_payload(), session=session, encryption_key=_KEY)
    await service.create_provider(
        _payload(host="other.sharepoint.com", enabled=False),
        session=session,
        encryption_key=_KEY,
    )
    assert len(await service.list_providers(session=session)) == 2
    assert len(await service.list_providers(enabled=True, session=session)) == 1
    assert len(await service.list_providers(host="other.sharepoint.com", session=session)) == 1
    assert (await service.list_providers(provider="sharepoint", session=session))[0]["provider"] == "sharepoint"


async def test_get_unknown_is_none(session: AsyncSession) -> None:
    assert await service.get_provider(uuid4(), session=session) is None


async def test_update_changes_only_supplied_fields(session: AsyncSession) -> None:
    created = await service.create_provider(_payload(), session=session, encryption_key=_KEY)
    provider_id = UUID(created["id"])
    updated = await service.update_provider(
        provider_id, {"enabled": False, "site_url": "https://new"}, session=session
    )
    assert updated is not None
    assert updated["enabled"] is False
    assert updated["site_url"] == "https://new"
    # Untouched fields keep their values, and the key is not disturbed.
    assert updated["tenant_id"] == "t"
    assert updated["has_private_key"] is True


async def test_update_rotates_private_key(session: AsyncSession) -> None:
    created = await service.create_provider(_payload(), session=session, encryption_key=_KEY)
    provider_id = UUID(created["id"])
    row_before = await session.get(AuthProviderConfig, provider_id)
    token_before = row_before.private_key_encrypted

    await service.update_provider(
        provider_id, {"private_key": "NEW-PEM"}, session=session, encryption_key=_KEY
    )
    await session.refresh(row_before)
    assert row_before.private_key_encrypted != token_before
    assert row_before.decrypted_private_key(_KEY) == "NEW-PEM"


async def test_update_clears_private_key_when_falsy(session: AsyncSession) -> None:
    created = await service.create_provider(_payload(), session=session, encryption_key=_KEY)
    provider_id = UUID(created["id"])
    updated = await service.update_provider(
        provider_id, {"private_key": None}, session=session, encryption_key=_KEY
    )
    assert updated is not None
    assert updated["has_private_key"] is False


async def test_update_unknown_is_none(session: AsyncSession) -> None:
    assert await service.update_provider(uuid4(), {"enabled": False}, session=session) is None


async def test_delete(session: AsyncSession) -> None:
    created = await service.create_provider(_payload(), session=session, encryption_key=_KEY)
    provider_id = UUID(created["id"])
    assert await service.delete_provider(provider_id, session=session) is True
    assert await service.get_provider(provider_id, session=session) is None
    assert await service.delete_provider(provider_id, session=session) is False
