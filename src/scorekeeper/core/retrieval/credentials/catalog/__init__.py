"""Credential-provider catalog — the home for concrete credential providers.

Add one module per provider here, each defining a ``CredentialProvider`` subclass decorated
with ``@register_credential_provider``, then import it below so that importing this package
registers every provider::

    from scorekeeper.core.retrieval.credentials.catalog import sharepoint  # noqa: F401
"""

from scorekeeper.core.retrieval.credentials.catalog import oauth2  # noqa: F401
from scorekeeper.core.retrieval.credentials.catalog import sharepoint  # noqa: F401
