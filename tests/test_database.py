"""Tests for ORM-level helpers in ``scorekeeper.database``."""

from __future__ import annotations

import pytest

from scorekeeper.database import AuthProviderConfig, RetrievedContextDocument
from scorekeeper.retrieval.credentials.secrets import SecretError
from scorekeeper.retrieved_context import RetrievedDocument


def test_from_document_maps_fields_and_rank() -> None:
    doc = RetrievedDocument(name="n", document="d.pdf", content="c", url="http://x")
    row = RetrievedContextDocument.from_document(doc, rank=3)
    assert (row.rank, row.name, row.document, row.content, row.url) == (
        3,
        "n",
        "d.pdf",
        "c",
        "http://x",
    )


def test_to_document_round_trips() -> None:
    doc = RetrievedDocument(name="n", document="d.pdf", content="c", url=None)
    assert RetrievedContextDocument.from_document(doc, rank=0).to_document() == doc


def test_auth_provider_from_sharepoint_encrypts_and_round_trips() -> None:
    key = "clave-maestra"
    row = AuthProviderConfig.from_sharepoint(
        host="h.sharepoint.com",
        tenant_id="t",
        client_id="c",
        thumbprint="th",
        site_url="https://h.sharepoint.com/sites/x",
        private_key="-----BEGIN PRIVATE KEY-----secret",
        encryption_key=key,
    )
    # The PEM is never stored in the clear; only the encrypted token + salt are.
    assert row.provider == "sharepoint"
    assert row.private_key_encrypted and row.private_key_salt
    assert "secret" not in (row.private_key_encrypted or "")
    assert row.decrypted_private_key(key) == "-----BEGIN PRIVATE KEY-----secret"


def test_auth_provider_decrypt_wrong_key_raises() -> None:
    row = AuthProviderConfig.from_sharepoint(
        host="h.sharepoint.com",
        tenant_id="t",
        client_id="c",
        thumbprint="th",
        site_url="https://h.sharepoint.com/sites/x",
        private_key="pem",
        encryption_key="clave-1",
    )
    with pytest.raises(SecretError):
        row.decrypted_private_key("clave-2")


def test_auth_provider_decrypt_without_stored_key_raises() -> None:
    with pytest.raises(SecretError):
        AuthProviderConfig(provider="sharepoint", host="h").decrypted_private_key("k")


def test_auth_provider_from_oauth2_encrypts_secret_and_stores_settings() -> None:
    key = "clave-maestra"
    row = AuthProviderConfig.from_oauth2(
        host="api.example.com",
        client_id="cid",
        client_secret="s3cr3t",
        token_url="https://idp/token",
        scope="files.read",
        encryption_key=key,
    )
    assert row.provider == "oauth2"
    # Non-secret OAuth2 config lands in ``settings``; the secret is encrypted.
    assert row.settings == {"token_url": "https://idp/token", "scope": "files.read"}
    assert "s3cr3t" not in (row.private_key_encrypted or "")
    # ``decrypted_secret`` is the generic accessor; the cert alias returns the same value.
    assert row.decrypted_secret(key) == "s3cr3t"
    assert row.decrypted_private_key(key) == "s3cr3t"
