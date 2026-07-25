"""Tests for the credential secret encryption helpers."""

from __future__ import annotations

import pytest

from scorekeeper.core.retrieval.credentials.secrets import (
    SecretError,
    decrypt_secret,
    encrypt_secret,
)

_KEY = "master-secret-clave-maestra"


def test_round_trip() -> None:
    salt, token = encrypt_secret("-----BEGIN PRIVATE KEY-----", _KEY)
    assert decrypt_secret(salt, token, _KEY) == "-----BEGIN PRIVATE KEY-----"


def test_salt_and_token_differ_per_call() -> None:
    # A fresh random salt each call means identical plaintext never yields identical output.
    salt1, token1 = encrypt_secret("same-pem", _KEY)
    salt2, token2 = encrypt_secret("same-pem", _KEY)
    assert salt1 != salt2
    assert token1 != token2
    assert decrypt_secret(salt1, token1, _KEY) == "same-pem"
    assert decrypt_secret(salt2, token2, _KEY) == "same-pem"


def test_wrong_key_fails_to_decrypt() -> None:
    salt, token = encrypt_secret("secreto", _KEY)
    with pytest.raises(SecretError):
        decrypt_secret(salt, token, "otra-clave")


def test_corrupt_token_fails() -> None:
    salt, _ = encrypt_secret("secreto", _KEY)
    with pytest.raises(SecretError):
        decrypt_secret(salt, "no-es-un-token-valido", _KEY)


def test_empty_master_key_is_error() -> None:
    with pytest.raises(SecretError):
        encrypt_secret("secreto", "")
