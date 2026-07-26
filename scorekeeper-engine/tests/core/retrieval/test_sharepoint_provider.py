"""Tests for the SharePoint certificate credential provider (no network)."""

from __future__ import annotations

import sys
import types

import pytest

from scorekeeper.db.models import AuthProviderConfig
from scorekeeper.core.retrieval.credentials.base import CredentialError
from scorekeeper.core.retrieval.credentials.catalog.sharepoint import SharePointCredentialProvider
from scorekeeper.core.retrieval.credentials.secrets import SecretError

_KEY = "clave-maestra"


class _FakeClientContext:
    """Stand-in for office365's ``ClientContext`` that records the credential call."""

    last_call: dict[str, object] = {}

    def __init__(self, site_url: str) -> None:
        _FakeClientContext.last_call = {"site_url": site_url}

    def with_client_certificate(self, **kwargs: object) -> "_FakeClientContext":
        _FakeClientContext.last_call.update(kwargs)
        return self


@pytest.fixture
def fake_office365(monkeypatch: pytest.MonkeyPatch) -> type[_FakeClientContext]:
    """Inject a fake ``office365.sharepoint.client_context`` so no SDK/network is needed."""
    root = types.ModuleType("office365")
    sub = types.ModuleType("office365.sharepoint")
    mod = types.ModuleType("office365.sharepoint.client_context")
    mod.ClientContext = _FakeClientContext  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "office365", root)
    monkeypatch.setitem(sys.modules, "office365.sharepoint", sub)
    monkeypatch.setitem(sys.modules, "office365.sharepoint.client_context", mod)
    _FakeClientContext.last_call = {}
    return _FakeClientContext


def _config() -> AuthProviderConfig:
    return AuthProviderConfig.from_sharepoint(
        host="cognitactix-my.sharepoint.com",
        tenant_id="tenant-1",
        client_id="client-1",
        thumbprint="THUMB",
        site_url="https://cognitactix-my.sharepoint.com/sites/x",
        private_key="-----BEGIN PRIVATE KEY-----abc",
        encryption_key=_KEY,
    )


def test_build_client_passes_cert_credentials(
    fake_office365: type[_FakeClientContext],
) -> None:
    client = SharePointCredentialProvider().build_client(_config(), encryption_key=_KEY)
    assert client.kind == "sharepoint"
    call = fake_office365.last_call
    assert call["site_url"] == "https://cognitactix-my.sharepoint.com/sites/x"
    assert call["tenant"] == "tenant-1"
    assert call["client_id"] == "client-1"
    assert call["thumbprint"] == "THUMB"
    # The private key handed to the SDK is the decrypted PEM, not the stored ciphertext.
    assert call["private_key"] == "-----BEGIN PRIVATE KEY-----abc"


def test_missing_required_field_raises(
    fake_office365: type[_FakeClientContext],
) -> None:
    config = _config()
    config.site_url = None
    with pytest.raises(CredentialError):
        SharePointCredentialProvider().build_client(config, encryption_key=_KEY)


def test_wrong_encryption_key_raises_secret_error(
    fake_office365: type[_FakeClientContext],
) -> None:
    with pytest.raises(SecretError):
        SharePointCredentialProvider().build_client(_config(), encryption_key="otra-clave")


def test_credentials_available_needs_no_sdk() -> None:
    # No office365 fixture here: availability must be decidable without importing the SDK.
    provider = SharePointCredentialProvider()
    assert provider.credentials_available(_config(), encryption_key=_KEY) is True
    assert provider.credentials_available(_config(), encryption_key="mala") is False
    incomplete = _config()
    incomplete.thumbprint = None
    assert provider.credentials_available(incomplete, encryption_key=_KEY) is False
