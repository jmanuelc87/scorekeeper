"""Tests for the OAuth2 credential provider and its client (no network)."""

from __future__ import annotations

import pytest

from scorekeeper.db.models import AuthProviderConfig
from scorekeeper.core.retrieval.credentials.base import AuthClient, CredentialError
from scorekeeper.core.retrieval.credentials.catalog.oauth2 import (
    OAuth2Client,
    OAuth2CredentialProvider,
)
from scorekeeper.core.retrieval.credentials.registry import CredentialProviderRegistry
from scorekeeper.core.retrieval.credentials.secrets import SecretError
from scorekeeper.core.retrieval.types import DocType, DocumentLocator

_KEY = "clave-maestra"


class _Resp:
    """Minimal httpx-style response stub."""

    def __init__(self, *, json_data: dict | None = None, content: bytes = b"", status: int = 200):
        self._json = json_data or {}
        self.content = content
        self._status = status

    def json(self) -> dict:
        return self._json

    def raise_for_status(self) -> None:
        if self._status >= 400:
            raise RuntimeError(f"HTTP {self._status}")


class _FakeHttp:
    """Records POST/GET calls; returns a fixed token then document bytes."""

    def __init__(self, *, token: dict | None = None, content: bytes = b"DOCBYTES"):
        self._token = token if token is not None else {"access_token": "tok-1", "expires_in": 3600}
        self._content = content
        self.posts: list[tuple[str, dict | None]] = []
        self.gets: list[tuple[str, dict | None]] = []

    def post(self, url: str, data: dict | None = None) -> _Resp:
        self.posts.append((url, data))
        return _Resp(json_data=self._token)

    def get(self, url: str, headers: dict | None = None) -> _Resp:
        self.gets.append((url, headers))
        return _Resp(content=self._content)


def _client(http: _FakeHttp, *, time_source=None) -> OAuth2Client:
    return OAuth2Client(
        token_url="https://idp/token",
        client_id="cid",
        client_secret="sec",
        scope="files.read",
        http_client=http,
        time_source=time_source,
    )


def _locator(host: str = "api.example.com") -> DocumentLocator:
    return DocumentLocator(
        document_url=f"https://{host}/doc/1",
        filename="1",
        doc_type=DocType.PDF,
        host=host,
    )


# -- client ---------------------------------------------------------------------------------


def test_client_conforms_to_authclient() -> None:
    assert isinstance(_client(_FakeHttp()), AuthClient)


def test_download_uses_client_credentials_and_bearer() -> None:
    http = _FakeHttp()
    assert _client(http).download(_locator()) == b"DOCBYTES"
    assert http.posts[0][1] == {
        "grant_type": "client_credentials",
        "client_id": "cid",
        "client_secret": "sec",
        "scope": "files.read",
    }
    assert http.gets[0][1] == {"Authorization": "Bearer tok-1"}


def test_token_is_cached_across_downloads() -> None:
    http = _FakeHttp()
    client = _client(http)
    client.download(_locator())
    client.download(_locator())
    assert len(http.posts) == 1  # token acquired once, reused
    assert len(http.gets) == 2


def test_token_refreshes_after_expiry() -> None:
    http = _FakeHttp(token={"access_token": "tok-1", "expires_in": 100})
    clock = {"t": 0.0}
    client = _client(http, time_source=lambda: clock["t"])
    client.download(_locator())
    clock["t"] = 500.0  # past expiry (100s - 30s skew)
    client.download(_locator())
    assert len(http.posts) == 2


def test_missing_access_token_raises() -> None:
    http = _FakeHttp(token={"expires_in": 3600})  # no access_token
    with pytest.raises(CredentialError):
        _client(http).download(_locator())


def test_scope_omitted_when_none() -> None:
    http = _FakeHttp()
    OAuth2Client(
        token_url="https://idp/token", client_id="c", client_secret="s", http_client=http
    ).download(_locator())
    assert "scope" not in (http.posts[0][1] or {})


# -- provider -------------------------------------------------------------------------------


def _row(**overrides) -> AuthProviderConfig:
    row = AuthProviderConfig.from_oauth2(
        host="api.example.com",
        client_id="cid",
        client_secret="sec",
        token_url="https://idp/token",
        scope="files.read",
        encryption_key=_KEY,
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


def test_provider_is_registered() -> None:
    assert CredentialProviderRegistry.get("oauth2").kind == "oauth2"


def test_build_client_returns_oauth2_client() -> None:
    client = OAuth2CredentialProvider().build_client(_row(), encryption_key=_KEY)
    assert isinstance(client, OAuth2Client)
    assert client.kind == "oauth2"


def test_build_client_missing_token_url_raises() -> None:
    with pytest.raises(CredentialError):
        OAuth2CredentialProvider().build_client(_row(settings={}), encryption_key=_KEY)


def test_build_client_missing_client_id_raises() -> None:
    with pytest.raises(CredentialError):
        OAuth2CredentialProvider().build_client(_row(client_id=None), encryption_key=_KEY)


def test_build_client_wrong_key_raises_secret_error() -> None:
    with pytest.raises(SecretError):
        OAuth2CredentialProvider().build_client(_row(), encryption_key="otra")


def test_credentials_available_needs_no_http() -> None:
    provider = OAuth2CredentialProvider()
    assert provider.credentials_available(_row(), encryption_key=_KEY) is True
    assert provider.credentials_available(_row(), encryption_key="mala") is False
    assert provider.credentials_available(_row(settings={}), encryption_key=_KEY) is False
