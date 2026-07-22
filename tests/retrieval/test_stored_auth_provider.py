"""Tests for the DB-backed Authorize stage (``StoredAuthProvider``)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from scorekeeper.database import AuthProviderConfig, Base
from scorekeeper.retrieval import (
    AuthClient,
    AuthProvider,
    AuthRequirement,
    AuthStatus,
    DocType,
    DocumentLocator,
    StoredAuthProvider,
)

_KEY = "clave-maestra"
_HOST = "cognitactix-my.sharepoint.com"


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _sharepoint_row(*, host: str = _HOST, enabled: bool = True) -> AuthProviderConfig:
    return AuthProviderConfig.from_sharepoint(
        host=host,
        tenant_id="tenant-1",
        client_id="client-1",
        thumbprint="THUMB",
        site_url=f"https://{host}/sites/x",
        private_key="-----BEGIN PRIVATE KEY-----abc",
        encryption_key=_KEY,
        enabled=enabled,
    )


def _locator(host: str) -> DocumentLocator:
    return DocumentLocator(
        document_url=f"https://{host}/x/a.pdf",
        filename="a.pdf",
        doc_type=DocType.PDF,
        host=host,
    )


def test_configured_host_is_satisfied(session: Session) -> None:
    session.add(_sharepoint_row())
    session.commit()
    provider = StoredAuthProvider(session=session, encryption_key=_KEY)
    decision = provider.classify(_locator(_HOST))
    assert decision.requirement is AuthRequirement.REQUIRED
    assert decision.status is AuthStatus.SATISFIED
    assert decision.provider == "sharepoint"
    assert provider.headers(_locator(_HOST)) == {}


def test_unconfigured_host_is_public(session: Session) -> None:
    session.add(_sharepoint_row())
    session.commit()
    provider = StoredAuthProvider(session=session, encryption_key=_KEY)
    decision = provider.classify(_locator("eleconomista.com.mx"))
    assert decision.requirement is AuthRequirement.PUBLIC
    assert decision.status is AuthStatus.NOT_NEEDED
    assert decision.provider is None


def test_disabled_row_is_ignored(session: Session) -> None:
    session.add(_sharepoint_row(enabled=False))
    session.commit()
    provider = StoredAuthProvider(session=session, encryption_key=_KEY)
    assert provider.classify(_locator(_HOST)).requirement is AuthRequirement.PUBLIC


def test_subdomain_matches_configured_host(session: Session) -> None:
    session.add(_sharepoint_row(host="sharepoint.com"))
    session.commit()
    provider = StoredAuthProvider(session=session, encryption_key=_KEY)
    # A subdomain of the configured host is gated; a look-alike suffix is not.
    assert provider.classify(_locator("a.b.sharepoint.com")).requirement is AuthRequirement.REQUIRED
    assert provider.classify(_locator("notsharepoint.com")).requirement is AuthRequirement.PUBLIC


def test_host_matching_is_case_insensitive(session: Session) -> None:
    session.add(_sharepoint_row())
    session.commit()
    provider = StoredAuthProvider(session=session, encryption_key=_KEY)
    assert provider.classify(_locator(_HOST.upper())).status is AuthStatus.SATISFIED


def test_missing_encryption_key_is_missing_credentials(session: Session) -> None:
    session.add(_sharepoint_row())
    session.commit()
    provider = StoredAuthProvider(session=session, encryption_key=None)
    decision = provider.classify(_locator(_HOST))
    assert decision.requirement is AuthRequirement.REQUIRED
    assert decision.status is AuthStatus.MISSING_CREDENTIALS


def test_wrong_encryption_key_is_missing_credentials(session: Session) -> None:
    session.add(_sharepoint_row())
    session.commit()
    provider = StoredAuthProvider(session=session, encryption_key="otra-clave")
    assert provider.classify(_locator(_HOST)).status is AuthStatus.MISSING_CREDENTIALS


def test_client_builds_generic_auth_client(session: Session, monkeypatch) -> None:
    import sys
    import types

    class _FakeCtx:
        def __init__(self, site_url: str) -> None:
            self.site_url = site_url

        def with_client_certificate(self, **kwargs: object) -> "_FakeCtx":
            return self

    mod = types.ModuleType("office365.sharepoint.client_context")
    mod.ClientContext = _FakeCtx  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "office365", types.ModuleType("office365"))
    monkeypatch.setitem(sys.modules, "office365.sharepoint", types.ModuleType("office365.sharepoint"))
    monkeypatch.setitem(sys.modules, "office365.sharepoint.client_context", mod)

    session.add(_sharepoint_row())
    session.commit()
    provider = StoredAuthProvider(session=session, encryption_key=_KEY)
    client = provider.client(_locator(_HOST))
    assert isinstance(client, AuthClient)
    assert client.kind == "sharepoint"
    # Public hosts get no client; the built client is cached per host.
    assert provider.client(_locator("eleconomista.com.mx")) is None
    assert provider.client(_locator(_HOST)) is client


def test_conforms_to_auth_provider_protocol(session: Session) -> None:
    assert isinstance(StoredAuthProvider(session=session, encryption_key=_KEY), AuthProvider)
