"""Symmetric encryption for stored credential secrets (certificate private keys).

A provider's certificate private key must be recoverable at run time to build its
authenticated client, so it is **encrypted** (reversible) rather than hashed (one-way).
Each stored secret carries its own random **salt**: a per-row Fernet key is derived from
the deployment's master secret (``settings.auth_encryption_key``) and that salt via
PBKDF2-HMAC-SHA256, then the plaintext is sealed with :class:`~cryptography.fernet.Fernet`
(the token bundles the nonce, ciphertext, and authentication tag).

Storing the salt per row means two rows holding the same key never share a derived key or
ciphertext, and rotating the master secret invalidates every stored token at once.
"""

from __future__ import annotations

import base64
import os

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# PBKDF2 work factor and salt size. 480_000 iterations matches current OWASP guidance for
# PBKDF2-HMAC-SHA256; the salt is 16 random bytes.
_KDF_ITERATIONS = 480_000
_SALT_BYTES = 16


class SecretError(Exception):
    """Raised when a secret cannot be encrypted or decrypted (bad key / corrupt token)."""


def _derive_key(master_key: str, salt: bytes) -> bytes:
    """Derive a url-safe base64 Fernet key from ``master_key`` and ``salt``."""
    if not master_key:
        raise SecretError("auth_encryption_key no está configurado")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_KDF_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(master_key.encode("utf-8")))


def encrypt_secret(plaintext: str, master_key: str) -> tuple[str, str]:
    """Encrypt ``plaintext`` under ``master_key`` with a fresh random salt.

    Returns ``(salt_b64, token)`` — both url-safe base64 strings ready for storage.
    """
    salt = os.urandom(_SALT_BYTES)
    token = Fernet(_derive_key(master_key, salt)).encrypt(plaintext.encode("utf-8"))
    return base64.urlsafe_b64encode(salt).decode("ascii"), token.decode("ascii")


def decrypt_secret(salt_b64: str, token: str, master_key: str) -> str:
    """Decrypt a ``token`` produced by :func:`encrypt_secret` using its stored salt.

    Raises :class:`SecretError` when the master key is wrong or the token is corrupt.
    """
    try:
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        plaintext = Fernet(_derive_key(master_key, salt)).decrypt(token.encode("utf-8"))
    except (InvalidToken, ValueError) as exc:
        raise SecretError("No se pudo descifrar el secreto almacenado") from exc
    return plaintext.decode("utf-8")
