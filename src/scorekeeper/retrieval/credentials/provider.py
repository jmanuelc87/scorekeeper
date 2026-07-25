"""DB-backed Authorize stage: resolve a locator against configured credential providers.

``StoredAuthProvider`` implements the
:class:`~scorekeeper.retrieval.protocols.AuthProvider` contract using the ``auth_providers``
table as its credential source (the piece the earlier ``HostRuleAuthProvider`` left injected
and empty). A locator whose host matches an **enabled** provider row is ``REQUIRED``; it
resolves to ``SATISFIED`` when that provider holds usable credentials (its secret decrypts
under the configured ``auth_encryption_key``) and ``MISSING_CREDENTIALS`` otherwise.

The stage exposes the pipeline's **generic client** via :meth:`client`: it dispatches on the
row's ``provider`` kind to the registered :class:`CredentialProvider`, which builds an
:class:`AuthClient` (a SharePoint ``ClientContext``, …). Clients are built lazily and cached
per host. Reading configuration touches the DB; building a client may import the backend SDK.
"""

from __future__ import annotations

from collections.abc import Mapping

from scorekeeper.config.settings import get_settings
from scorekeeper.db.connection import session_scope
from scorekeeper.db.models import AuthProviderConfig
from scorekeeper.db.repositories import auth_providers as repo
from scorekeeper.retrieval.credentials import catalog as _catalog  # noqa: F401  (populate registry)
from scorekeeper.retrieval.credentials.base import AuthClient, CredentialError
from scorekeeper.retrieval.credentials.registry import CredentialProviderRegistry
from scorekeeper.retrieval.credentials.secrets import SecretError
from scorekeeper.retrieval.types import (
    AuthDecision,
    AuthRequirement,
    AuthStatus,
    DocumentLocator,
)
from sqlalchemy.ext.asyncio import AsyncSession


class StoredAuthProvider:
    """Authorize stage backed by the ``auth_providers`` table and the provider registry.

    ``session`` defaults to a fresh ``SessionLocal()`` per lookup and ``encryption_key`` to
    ``settings.auth_encryption_key`` (the injection convention used across the codebase, so
    tests can pass an in-memory session and an explicit key). Enabled rows are loaded once
    and cached; built clients are cached per host.
    """

    def __init__(
        self,
        *,
        session: AsyncSession | None = None,
        encryption_key: str | None = None,
    ) -> None:
        self._session = session
        self._encryption_key = (
            encryption_key if encryption_key is not None
            else get_settings().auth_encryption_key
        )
        self._configs: list[AuthProviderConfig] | None = None
        self._clients: dict[str, AuthClient] = {}

    # -- AuthProvider protocol ----------------------------------------------------------

    async def classify(self, locator: DocumentLocator) -> AuthDecision:
        """Classify the auth requirement/status for ``locator``'s host against the DB."""
        config = await self._match(locator.host)
        if config is None:
            return AuthDecision(
                requirement=AuthRequirement.PUBLIC,
                status=AuthStatus.NOT_NEEDED,
                provider=None,
            )
        status = (
            AuthStatus.SATISFIED
            if self._credentials_available(config)
            else AuthStatus.MISSING_CREDENTIALS
        )
        return AuthDecision(
            requirement=AuthRequirement.REQUIRED,
            status=status,
            provider=config.provider,
        )

    def headers(self, locator: DocumentLocator) -> Mapping[str, str]:
        """No header-based auth here — credentials are carried by the generic client."""
        return {}

    async def client(self, locator: DocumentLocator) -> AuthClient | None:
        """Build (and cache) the generic :class:`AuthClient` for ``locator``'s host.

        Returns ``None`` for public hosts. Raises :class:`CredentialError` (or
        ``SecretError``) when a gated host is configured but its client cannot be built —
        this is the fetch-time failure, distinct from the classify-time status.
        """
        config = await self._match(locator.host)
        if config is None:
            return None
        cached = self._clients.get(config.host)
        if cached is not None:
            return cached
        if self._encryption_key is None:
            raise CredentialError("auth_encryption_key no está configurado")
        provider = CredentialProviderRegistry.create(config.provider)
        client = provider.build_client(config, encryption_key=self._encryption_key)
        self._clients[config.host] = client
        return client

    # -- helpers ------------------------------------------------------------------------

    async def _enabled_configs(self) -> list[AuthProviderConfig]:
        """Load and cache the enabled ``auth_providers`` rows."""
        if self._configs is None:
            async with session_scope(self._session) as db:
                self._configs = await repo.list_enabled(db)
        return self._configs

    async def _match(self, host: str) -> AuthProviderConfig | None:
        """Return the enabled config gating ``host`` (exact or parent-domain), longest wins.

        A row's ``host`` matches a locator host equal to it or a subdomain of it; when
        several rows match, the most specific (longest ``host``) is chosen.
        """
        host = host.lower()
        matches = [
            config
            for config in await self._enabled_configs()
            if (h := config.host.lower()) and (host == h or host.endswith(f".{h}"))
        ]
        if not matches:
            return None
        return max(matches, key=lambda config: len(config.host))

    def _credentials_available(self, config: AuthProviderConfig) -> bool:
        """Whether ``config``'s provider holds usable credentials (no backend SDK needed)."""
        if self._encryption_key is None:
            return False
        try:
            provider = CredentialProviderRegistry.create(config.provider)
        except KeyError:
            return False
        try:
            return provider.credentials_available(config, encryption_key=self._encryption_key)
        except SecretError:
            return False
