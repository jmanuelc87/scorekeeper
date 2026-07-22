"""SharePoint credential provider: certificate-based ``ClientContext``.

Authenticates against SharePoint Online with an Azure AD app client certificate, following
``ClientContext(site_url).with_client_certificate(tenant, client_id, thumbprint,
private_key)``. The ``office365`` SDK is imported lazily so the retrieval taxonomy stays
importable without the optional ``retrieval`` extra; only building a client requires it.

The certificate private key lives encrypted on the ``auth_providers`` row and is decrypted
with the deployment master secret just before it is handed to ``with_client_certificate``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import unquote, urlsplit

from scorekeeper.retrieval.credentials.base import AuthClient, CredentialError, CredentialProvider
from scorekeeper.retrieval.credentials.registry import register_credential_provider
from scorekeeper.retrieval.types import DocumentLocator

if TYPE_CHECKING:
    from scorekeeper.database import AuthProviderConfig

# Settings every SharePoint row must supply to build a certificate ClientContext.
_REQUIRED_FIELDS = ("tenant_id", "client_id", "thumbprint", "site_url")


class SharePointClient:
    """:class:`AuthClient` wrapping an authenticated SharePoint ``ClientContext``.

    ``download`` resolves the located document to a server-relative URL and streams its
    bytes through the certificate-authenticated context.
    """

    kind = "sharepoint"

    def __init__(self, context: Any) -> None:
        self._context = context

    @property
    def context(self) -> Any:
        """The underlying office365 ``ClientContext`` (for the fetch stage / debugging)."""
        return self._context

    def download(self, locator: DocumentLocator) -> bytes:
        """Fetch the document's bytes via ``Web.get_file_by_server_relative_url``."""
        from office365.sharepoint.files.file import File

        server_relative_url = unquote(urlsplit(locator.document_url).path)
        response = File.open_binary(self._context, server_relative_url)
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
        context = ClientContext(config.site_url).with_client_certificate(
            tenant=config.tenant_id,
            client_id=config.client_id,
            thumbprint=config.thumbprint,
            private_key=private_key,
        )
        return SharePointClient(context)
