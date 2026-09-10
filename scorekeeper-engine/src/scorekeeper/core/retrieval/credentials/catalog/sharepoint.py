"""SharePoint credential provider: certificate-based ``ClientContext``.

Authenticates against SharePoint Online with an Azure AD app client certificate, following
``ClientContext(site).with_client_certificate(tenant, client_id, thumbprint,
private_key)``. The ``office365`` SDK is imported lazily so the retrieval taxonomy stays
importable without the optional ``retrieval`` extra; only building a client requires it.

**The document URL decides the site, the row decides the credentials.** SharePoint scopes
``_api`` to a site collection, so the context is built per download from the site root of
``locator.document_url`` — not from the row's ``site_url``, which would otherwise be appended
in front of every reference's path and send a ``/personal/…`` document at a ``/sites/…`` API.
Contexts are cached per site root, so the certificate handshake happens once per site.

The certificate private key lives encrypted on the ``auth_providers`` row and is decrypted
with the deployment master secret just before it is handed to ``with_client_certificate``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import unquote, urlsplit

from scorekeeper.core.retrieval.credentials.base import AuthClient, CredentialError, CredentialProvider
from scorekeeper.core.retrieval.credentials.registry import register_credential_provider
from scorekeeper.core.retrieval.types import DocumentLocator

if TYPE_CHECKING:
    from scorekeeper.db.models import AuthProviderConfig

# Settings every SharePoint row must supply to build a certificate ClientContext.
_REQUIRED_FIELDS = ("tenant_id", "client_id", "thumbprint")

# SharePoint's managed paths: the first two segments name the site collection under them.
_MANAGED_PATHS = ("sites", "teams", "personal")


def _site_root(document_url: str) -> str:
    """The site collection ``document_url`` lives in — what ``_api`` must be rooted at."""
    parts = urlsplit(document_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) >= 2 and segments[0].lower() in _MANAGED_PATHS:
        return f"{origin}/{segments[0]}/{segments[1]}"
    return origin


class SharePointClient:
    """:class:`AuthClient` wrapping certificate-authenticated SharePoint ``ClientContext``s.

    ``download`` resolves the located document to a server-relative URL and streams its
    bytes through the context of the site collection that document's own URL names.
    """

    kind = "sharepoint"

    def __init__(self, context_factory: Callable[[str], Any]) -> None:
        self._context_factory = context_factory
        # One authenticated context per site root, reused across documents in that site.
        self._contexts: dict[str, Any] = {}

    def _context_for(self, document_url: str) -> Any:
        site_root = _site_root(document_url)
        context = self._contexts.get(site_root)
        if context is None:
            context = self._context_factory(site_root)
            self._contexts[site_root] = context
        return context

    def download(self, locator: DocumentLocator) -> bytes:
        """Fetch the document's bytes via ``Web.get_file_by_server_relative_url``."""
        from office365.sharepoint.files.file import File

        context = self._context_for(locator.document_url)
        server_relative_url = unquote(urlsplit(locator.document_url).path)
        response = File.open_binary(context, server_relative_url)
        return response.content


@register_credential_provider
class SharePointCredentialProvider(CredentialProvider):
    """Build a certificate-authenticated SharePoint client from a stored row."""

    kind: ClassVar[str] = "sharepoint"

    def require_settings(self, config: AuthProviderConfig) -> None:
        missing = [field for field in _REQUIRED_FIELDS if not getattr(config, field, None)]
        if missing:
            raise CredentialError(
                f"El proveedor SharePoint para {config.host} no configura: {', '.join(missing)}"
            )

    def build_client(
        self, config: AuthProviderConfig, *, encryption_key: str
    ) -> AuthClient:
        self.require_settings(config)

        try:
            from office365.sharepoint.client_context import ClientContext
        except ImportError as exc:  # pragma: no cover - exercised only without the extra.
            raise CredentialError(
                "El extra 'retrieval' (Office365-REST-Python-Client) no está instalado"
            ) from exc

        # Decrypt just before use; raises SecretError when the key is missing/wrong.
        private_key = config.decrypted_private_key(encryption_key)

        def build_context(site_url: str) -> Any:
            return ClientContext(site_url).with_client_certificate(
                tenant=config.tenant_id,
                client_id=config.client_id,
                thumbprint=config.thumbprint,
                private_key=private_key,
            )

        return SharePointClient(build_context)
