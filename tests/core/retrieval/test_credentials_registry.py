"""Tests for the credential-provider registry."""

from __future__ import annotations

import pytest

from scorekeeper.core.retrieval.credentials.base import CredentialProvider
from scorekeeper.core.retrieval.credentials.registry import (
    CredentialProviderRegistry,
    register_credential_provider,
)


def test_sharepoint_provider_is_registered() -> None:
    # Importing the credentials package imports the catalog, registering every provider.
    import scorekeeper.core.retrieval.credentials  # noqa: F401

    provider_cls = CredentialProviderRegistry.get("sharepoint")
    assert provider_cls.kind == "sharepoint"
    assert isinstance(CredentialProviderRegistry.create("sharepoint"), CredentialProvider)


def test_unknown_kind_raises() -> None:
    with pytest.raises(KeyError):
        CredentialProviderRegistry.get("no-existe")


def test_register_and_duplicate_detection() -> None:
    class _Dummy(CredentialProvider):
        kind = "dummy-test-kind"

        def build_client(self, config, *, encryption_key):  # pragma: no cover - not called
            raise NotImplementedError

    try:
        register_credential_provider(_Dummy)
        assert CredentialProviderRegistry.get("dummy-test-kind") is _Dummy

        class _Other(CredentialProvider):
            kind = "dummy-test-kind"

            def build_client(self, config, *, encryption_key):  # pragma: no cover
                raise NotImplementedError

        with pytest.raises(ValueError):
            register_credential_provider(_Other)
    finally:
        CredentialProviderRegistry._providers.pop("dummy-test-kind", None)


def test_missing_kind_raises() -> None:
    class _NoKind(CredentialProvider):
        def build_client(self, config, *, encryption_key):  # pragma: no cover
            raise NotImplementedError

    with pytest.raises(ValueError):
        register_credential_provider(_NoKind)
