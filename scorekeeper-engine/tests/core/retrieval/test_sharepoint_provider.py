"""Tests for the SharePoint certificate credential provider (no network)."""

from __future__ import annotations

import sys
import types

import pytest

from scorekeeper.db.models import AuthProviderConfig
from scorekeeper.core.retrieval.credentials.base import CredentialError
from scorekeeper.core.retrieval.credentials.catalog.sharepoint import SharePointCredentialProvider
from scorekeeper.core.retrieval.credentials.secrets import SecretError
from scorekeeper.core.retrieval.types import DocType, DocumentLocator

_KEY = "clave-maestra"
_HOST = "cognitactix-my.sharepoint.com"


class _FakeClientContext:
    """Stand-in for office365's ``ClientContext`` that records the credential call."""

    calls: list[dict[str, object]] = []

    def __init__(self, site_url: str) -> None:
        self.site_url = site_url
        _FakeClientContext.calls.append({"site_url": site_url})

    def with_client_certificate(self, **kwargs: object) -> "_FakeClientContext":
        _FakeClientContext.calls[-1].update(kwargs)
        return self

    @classmethod
    def last_call(cls) -> dict[str, object]:
        return cls.calls[-1]


class _FakeResponse:
    content = b"DOCBYTES"


class _FakeFile:
    """Stand-in for office365's ``File``, recording what each download asked for."""

    opened: list[tuple[str, str]] = []

    @staticmethod
    def open_binary(context: _FakeClientContext, server_relative_url: str) -> _FakeResponse:
        _FakeFile.opened.append((context.site_url, server_relative_url))
        return _FakeResponse()


@pytest.fixture
def fake_office365(monkeypatch: pytest.MonkeyPatch) -> type[_FakeClientContext]:
    """Inject a fake ``office365`` SDK so no real SDK/network is needed."""
    root = types.ModuleType("office365")
    sub = types.ModuleType("office365.sharepoint")
    ctx_mod = types.ModuleType("office365.sharepoint.client_context")
    ctx_mod.ClientContext = _FakeClientContext  # type: ignore[attr-defined]
    files_mod = types.ModuleType("office365.sharepoint.files")
    file_mod = types.ModuleType("office365.sharepoint.files.file")
    file_mod.File = _FakeFile  # type: ignore[attr-defined]
    for name, module in (
        ("office365", root),
        ("office365.sharepoint", sub),
        ("office365.sharepoint.client_context", ctx_mod),
        ("office365.sharepoint.files", files_mod),
        ("office365.sharepoint.files.file", file_mod),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    _FakeClientContext.calls = []
    _FakeFile.opened = []
    return _FakeClientContext


def _config() -> AuthProviderConfig:
    return AuthProviderConfig.from_sharepoint(
        host=_HOST,
        tenant_id="tenant-1",
        client_id="client-1",
        thumbprint="THUMB",
        site_url="https://cognitactix-my.sharepoint.com/sites/x",
        private_key="-----BEGIN PRIVATE KEY-----abc",
        encryption_key=_KEY,
    )


def _locator(url: str) -> DocumentLocator:
    return DocumentLocator(
        document_url=url,
        filename=url.rsplit("/", 1)[-1],
        doc_type=DocType.PDF,
        scheme="https",
        host=_HOST,
    )


def test_build_client_passes_cert_credentials(
    fake_office365: type[_FakeClientContext],
) -> None:
    client = SharePointCredentialProvider().build_client(_config(), encryption_key=_KEY)
    assert client.kind == "sharepoint"
    # No context is built until a document names the site it belongs to.
    assert fake_office365.calls == []

    client.download(_locator(f"https://{_HOST}/personal/u/Documents/a.pdf"))
    call = fake_office365.last_call()
    assert call["tenant"] == "tenant-1"
    assert call["client_id"] == "client-1"
    assert call["thumbprint"] == "THUMB"
    # The private key handed to the SDK is the decrypted PEM, not the stored ciphertext.
    assert call["private_key"] == "-----BEGIN PRIVATE KEY-----abc"


def test_download_roots_the_context_at_the_documents_own_site(
    fake_office365: type[_FakeClientContext],
) -> None:
    client = SharePointCredentialProvider().build_client(_config(), encryption_key=_KEY)
    assert client.download(_locator(f"https://{_HOST}/personal/u/Documents/a.pdf")) == b"DOCBYTES"
    # The row's site_url (…/sites/x) is not what the API is rooted at.
    assert _FakeFile.opened == [
        (f"https://{_HOST}/personal/u", "/personal/u/Documents/a.pdf")
    ]


def test_download_roots_at_the_host_without_a_managed_path(
    fake_office365: type[_FakeClientContext],
) -> None:
    client = SharePointCredentialProvider().build_client(_config(), encryption_key=_KEY)
    client.download(_locator(f"https://{_HOST}/Shared%20Documents/a.pdf"))
    assert _FakeFile.opened == [(f"https://{_HOST}", "/Shared Documents/a.pdf")]


def test_context_is_cached_per_site_root(
    fake_office365: type[_FakeClientContext],
) -> None:
    client = SharePointCredentialProvider().build_client(_config(), encryption_key=_KEY)
    client.download(_locator(f"https://{_HOST}/sites/a/Docs/one.pdf"))
    client.download(_locator(f"https://{_HOST}/sites/a/Docs/two.pdf"))
    client.download(_locator(f"https://{_HOST}/sites/b/Docs/three.pdf"))
    assert [call["site_url"] for call in fake_office365.calls] == [
        f"https://{_HOST}/sites/a",
        f"https://{_HOST}/sites/b",
    ]


def test_site_url_is_optional(fake_office365: type[_FakeClientContext]) -> None:
    config = _config()
    config.site_url = None
    client = SharePointCredentialProvider().build_client(config, encryption_key=_KEY)
    assert client.download(_locator(f"https://{_HOST}/sites/a/Docs/one.pdf")) == b"DOCBYTES"


def test_missing_required_field_raises(
    fake_office365: type[_FakeClientContext],
) -> None:
    config = _config()
    config.thumbprint = None
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
