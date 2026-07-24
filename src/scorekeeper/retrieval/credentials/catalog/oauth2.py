"""OAuth2 credential provider: a bearer-token client via the client-credentials grant.

Authenticates to an OAuth2 token endpoint with the **client-credentials** grant
(machine-to-machine — the only flow that fits headless, server-side document fetching) and
retrieves documents over HTTP with the resulting bearer token. ``OAuth2Client`` is a concrete
:class:`~scorekeeper.retrieval.credentials.base.AuthClient`, sibling to ``SharePointClient``.

The HTTP client (``httpx2``) is imported lazily and can be injected, so the taxonomy stays
importable and unit-testable without network I/O. The token is cached and refreshed shortly
before it expires. The row stores the OAuth2 endpoint/scope in ``settings`` and the client
secret in the shared encrypted secret columns.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

from scorekeeper.retrieval.credentials.base import (
    AuthClient,
    CredentialError,
    CredentialProvider,
)
from scorekeeper.retrieval.credentials.registry import register_credential_provider
from scorekeeper.retrieval.types import DocumentLocator

if TYPE_CHECKING:
    from scorekeeper.database import AuthProviderConfig

# Refresh a little before the token's stated expiry, to avoid using one that lapses in flight.
_EXPIRY_SKEW_SECONDS = 30.0
# Fallback token lifetime when the token response omits ``expires_in``.
_DEFAULT_EXPIRES_IN = 3600.0


class OAuth2Client:
    """:class:`AuthClient` that fetches with an OAuth2 client-credentials bearer token.

    Acquires a token from ``token_url`` (caching it until just before expiry) and downloads a
    located document with an ``Authorization: Bearer`` header. ``http_client`` (an httpx-style
    client with ``post``/``get``) and ``time_source`` are injectable for tests; by default a
    lazily-built ``httpx2.Client`` and ``time.monotonic`` are used.
    """

    kind = "oauth2"

    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        client_secret: str,
        scope: str | None = None,
        http_client: Any | None = None,
        time_source: Callable[[], float] | None = None,
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._http_client = http_client
        self._time = time_source or time.monotonic
        self._token: str | None = None
        self._expires_at = 0.0

    @property
    def http_client(self) -> Any:
        """The underlying HTTP client, building a default ``httpx2.Client`` on first use."""
        if self._http_client is None:
            import httpx2

            self._http_client = httpx2.Client()
        return self._http_client

    def _bearer(self) -> str:
        """Return a valid access token, acquiring/refreshing it when needed."""
        if self._token is not None and self._time() < self._expires_at:
            return self._token
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        if self._scope:
            data["scope"] = self._scope
        response = self.http_client.post(self._token_url, data=data)
        response.raise_for_status()
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise CredentialError(
                "La respuesta del endpoint OAuth2 no incluye 'access_token'"
            )
        expires_in = float(payload.get("expires_in", _DEFAULT_EXPIRES_IN))
        self._token = token
        self._expires_at = self._time() + max(0.0, expires_in - _EXPIRY_SKEW_SECONDS)
        return token

    def download(self, locator: DocumentLocator) -> bytes:
        """Fetch the document bytes with the bearer token."""
        response = self.http_client.get(
            locator.document_url,
            headers={"Authorization": f"Bearer {self._bearer()}"},
        )
        response.raise_for_status()
        return response.content


# Non-secret settings an OAuth2 row must supply (column, then ``settings`` keys).
_REQUIRED_FIELDS = ("client_id",)
_REQUIRED_SETTINGS = ("token_url",)


@register_credential_provider
class OAuth2CredentialProvider(CredentialProvider):
    """Build an :class:`OAuth2Client` from a stored ``oauth2`` row."""

    kind: ClassVar[str] = "oauth2"

    def require_settings(self, config: AuthProviderConfig) -> None:
        settings = config.settings or {}
        missing = [field for field in _REQUIRED_FIELDS if not getattr(config, field, None)]
        missing += [f"settings.{key}" for key in _REQUIRED_SETTINGS if not settings.get(key)]
        if missing:
            raise CredentialError(
                f"El proveedor OAuth2 para {config.host} no configura: {', '.join(missing)}"
            )

    def build_client(
        self, config: AuthProviderConfig, *, encryption_key: str
    ) -> AuthClient:
        self.require_settings(config)
        settings = config.settings or {}
        # Decrypt just before use; raises SecretError when the key is missing/wrong.
        client_secret = config.decrypted_secret(encryption_key)
        return OAuth2Client(
            token_url=settings["token_url"],
            client_id=config.client_id,
            client_secret=client_secret,
            scope=settings.get("scope"),
        )
