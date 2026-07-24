"""Database-backed credential taxonomy for the retrieval pipeline's authorize stage.

The authorize stage needs credentials to fetch gated documents. This package supplies them
from the ``auth_providers`` table: a taxonomy of :class:`CredentialProvider` classes (one per
*kind*, registered under ``catalog/``) that turn a stored row into a backend-agnostic
:class:`AuthClient`. ``StoredAuthProvider`` is the concrete authorize stage that reads the
table, resolves each host's ``AuthStatus``, and hands the pipeline the generic client.

Importing this package imports ``catalog`` so every provider registers itself. Certificate
private keys are stored encrypted (:mod:`~scorekeeper.retrieval.credentials.secrets`).
"""

from scorekeeper.retrieval.credentials.base import (
    AuthClient,
    CredentialError,
    CredentialProvider,
)
from scorekeeper.retrieval.credentials.registry import (
    CredentialProviderRegistry,
    register_credential_provider,
)
from scorekeeper.retrieval.credentials.secrets import (
    SecretError,
    decrypt_secret,
    encrypt_secret,
)
from scorekeeper.retrieval.credentials import catalog  # noqa: F401  (populate registry)
from scorekeeper.retrieval.credentials.provider import StoredAuthProvider

__all__ = [
    "AuthClient",
    "CredentialError",
    "CredentialProvider",
    "CredentialProviderRegistry",
    "register_credential_provider",
    "SecretError",
    "encrypt_secret",
    "decrypt_secret",
    "StoredAuthProvider",
]
