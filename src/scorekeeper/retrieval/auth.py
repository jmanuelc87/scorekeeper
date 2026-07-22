"""Concrete Authorize stage: classify a document's auth requirement and apply credentials.

``HostRuleAuthProvider`` implements the
:class:`~scorekeeper.retrieval.protocols.AuthProvider` contract —
``classify(locator) -> AuthDecision`` and ``headers(locator) -> Mapping[str, str]``. It is
the third concrete stage of the retrieval pipeline (see ``docs/retrieval-pipeline.md``),
between locate and fetch; the later stages (fetch/extract) remain deferred.

The requirement is decided by a **host rule**: a locator whose host matches one of the
gated domains (SharePoint by default) is ``REQUIRED``, everything else is ``PUBLIC``. The
requirement is then resolved against an **injected credential store** (host → bearer token):

* ``PUBLIC`` host → ``NOT_NEEDED``
* ``REQUIRED`` host with a credential → ``SATISFIED`` (``headers`` carries ``Authorization``)
* ``REQUIRED`` host without a credential → ``MISSING_CREDENTIALS``

Credentials are *consumed*, never minted here: the provider is stateless and does no network
I/O. Acquiring SharePoint/Azure AD tokens (OAuth) and populating the store is a later phase —
no credentials are wired in by default, so gated documents resolve to ``MISSING_CREDENTIALS``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from scorekeeper.retrieval.types import (
    AuthDecision,
    AuthRequirement,
    AuthStatus,
    DocumentLocator,
)

if TYPE_CHECKING:
    from scorekeeper.retrieval.credentials.base import AuthClient

# Hosts under these domains gate their documents behind authentication by default.
DEFAULT_GATED_DOMAINS = ("sharepoint.com",)

# Identifier recorded on the ``AuthDecision`` when a host rule matches.
DEFAULT_PROVIDER_NAME = "host-rule"


class HostRuleAuthProvider:
    """Classify a locator's host against gated-domain rules and apply injected credentials.

    ``gated_domains`` are matched as suffixes (a host equal to a domain or a subdomain of it
    is gated). ``credentials`` maps a host to its bearer token; it may be injected (tests, or
    a future credential source) and defaults to empty, so gated hosts resolve to
    ``MISSING_CREDENTIALS`` until credentials are wired in. Stateless and side-effect-free.
    """

    def __init__(
        self,
        *,
        gated_domains: Iterable[str] = DEFAULT_GATED_DOMAINS,
        credentials: Mapping[str, str] | None = None,
        provider_name: str = DEFAULT_PROVIDER_NAME,
    ) -> None:
        # Normalize domains to lowercase, dot-stripped suffixes for matching.
        self._gated_domains = tuple(
            domain.strip().strip(".").lower() for domain in gated_domains if domain.strip()
        )
        self._credentials = dict(credentials or {})
        self._provider_name = provider_name

    # -- AuthProvider protocol ----------------------------------------------------------

    def classify(self, locator: DocumentLocator) -> AuthDecision:
        """Classify the auth requirement/status for ``locator``'s host."""
        if not self._is_gated(locator.host):
            return AuthDecision(
                requirement=AuthRequirement.PUBLIC,
                status=AuthStatus.NOT_NEEDED,
                provider=None,
            )
        status = (
            AuthStatus.SATISFIED
            if self._credential_for(locator.host) is not None
            else AuthStatus.MISSING_CREDENTIALS
        )
        return AuthDecision(
            requirement=AuthRequirement.REQUIRED,
            status=status,
            provider=self._provider_name,
        )

    def headers(self, locator: DocumentLocator) -> Mapping[str, str]:
        """Auth headers for the fetch; empty unless the status resolves to ``SATISFIED``."""
        if self.classify(locator).status is AuthStatus.SATISFIED:
            token = self._credential_for(locator.host)
            if token:
                return {"Authorization": f"Bearer {token}"}
        return {}

    def client(self, locator: DocumentLocator) -> AuthClient | None:
        """No generic client — this provider carries credentials via ``headers``."""
        return None

    # -- helpers ------------------------------------------------------------------------

    def _is_gated(self, host: str) -> bool:
        """Whether ``host`` falls under a gated domain (exact match or subdomain)."""
        host = host.lower()
        return any(
            host == domain or host.endswith(f".{domain}") for domain in self._gated_domains
        )

    def _credential_for(self, host: str) -> str | None:
        """Return the bearer token held for ``host``, or ``None`` when none is available."""
        return self._credentials.get(host.lower()) or None
