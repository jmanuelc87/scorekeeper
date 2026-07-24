"""Contracts for the credential-provider taxonomy.

A :class:`CredentialProvider` turns a stored ``auth_providers`` row into an
:class:`AuthClient` — the **generic client** the retrieval pipeline consumes. The pipeline
never names SharePoint (or any other backend): it asks the authorize stage for an
``AuthClient`` and calls :meth:`AuthClient.download`, and the concrete provider's client
translates that into whatever its backend needs (a SharePoint ``ClientContext`` request, an
HTTP GET, …). Concrete providers register themselves in
``scorekeeper.retrieval.credentials.registry`` and live under ``catalog/``.

These contracts carry **no** backend SDK import; a provider imports its SDK lazily inside
:meth:`CredentialProvider.build_client`, so the taxonomy stays importable without the
optional ``retrieval`` extra installed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar, Protocol, runtime_checkable

from scorekeeper.retrieval.credentials.secrets import SecretError
from scorekeeper.retrieval.types import DocumentLocator

if TYPE_CHECKING:
    from scorekeeper.database import AuthProviderConfig


class CredentialError(Exception):
    """Raised when a provider cannot build its client (missing settings/key, bad config)."""


@runtime_checkable
class AuthClient(Protocol):
    """A backend-agnostic authenticated client the fetch stage retrieves documents through.

    ``kind`` identifies the provider that built it (e.g. ``"sharepoint"``). ``download``
    fetches the located document's raw bytes using the underlying authenticated backend.
    """

    kind: str

    def download(self, locator: DocumentLocator) -> bytes:
        """Fetch ``locator``'s document bytes through the authenticated backend."""
        ...


class CredentialProvider(ABC):
    """Builds an :class:`AuthClient` from a stored ``auth_providers`` row.

    A concrete provider declares the ``kind`` it handles (matched against the row's
    ``provider`` discriminator) and implements :meth:`build_client`, decrypting the row's
    secret with ``encryption_key`` and constructing its backend client. Building the client
    performs no network I/O beyond what the backend SDK defers until first use.
    """

    kind: ClassVar[str]

    def credentials_available(
        self, config: AuthProviderConfig, *, encryption_key: str
    ) -> bool:
        """Whether ``config``'s credentials can be materialized — **no backend SDK needed**.

        Used by the authorize stage to resolve ``SATISFIED`` vs ``MISSING_CREDENTIALS``
        without importing the (optional) SDK: it checks the required non-secret settings are
        present and the stored private key decrypts under ``encryption_key``.
        """
        try:
            self.require_settings(config)
            config.decrypted_secret(encryption_key)
        except (CredentialError, SecretError):
            return False
        return True

    def require_settings(self, config: AuthProviderConfig) -> None:
        """Validate the non-secret settings this provider needs; override to enforce fields.

        Raises :class:`CredentialError` listing what is missing. Default: no requirements.
        """

    @abstractmethod
    def build_client(self, config: AuthProviderConfig, *, encryption_key: str) -> AuthClient:
        """Build the authenticated :class:`AuthClient` for ``config``.

        Raises :class:`CredentialError` when required settings are absent or the SDK is not
        installed, and ``SecretError`` when the stored private key cannot be decrypted.
        """
