"""Runtime registry of credential-provider classes.

Concrete providers register themselves with the ``@register_credential_provider``
decorator, co-located with their definition under ``catalog/``. Importing the ``catalog``
package imports every provider module, which populates this registry. The DB-backed
authorize stage (``scorekeeper.retrieval.credentials.provider``) looks a provider up by the
``provider`` discriminator stored on an ``auth_providers`` row.

Mirrors ``scorekeeper.metrics.registry`` — the codebase's established taxonomy pattern.
"""

from __future__ import annotations

from typing import TypeVar

from scorekeeper.retrieval.credentials.base import CredentialProvider

_P = TypeVar("_P", bound=CredentialProvider)


class CredentialProviderRegistry:
    """A kind → credential-provider-class registry populated at import time."""

    _providers: dict[str, type[CredentialProvider]] = {}

    @classmethod
    def add(cls, provider_cls: type[CredentialProvider]) -> None:
        kind = getattr(provider_cls, "kind", None)
        if not kind:
            raise ValueError(f"El proveedor {provider_cls.__name__} no define 'kind'")
        existing = cls._providers.get(kind)
        if existing is not None and existing is not provider_cls:
            raise ValueError(f"Proveedor de credenciales duplicado: {kind}")
        cls._providers[kind] = provider_cls

    @classmethod
    def get(cls, kind: str) -> type[CredentialProvider]:
        try:
            return cls._providers[kind]
        except KeyError:
            raise KeyError(f"Proveedor de credenciales desconocido: {kind}") from None

    @classmethod
    def create(cls, kind: str) -> CredentialProvider:
        return cls.get(kind)()

    @classmethod
    def all(cls) -> list[type[CredentialProvider]]:
        return list(cls._providers.values())

    @classmethod
    def clear(cls) -> None:
        """Reset the registry — primarily for isolated tests."""
        cls._providers.clear()


def register_credential_provider(provider_cls: type[_P]) -> type[_P]:
    """Register a :class:`CredentialProvider` subclass by its ``kind``."""
    CredentialProviderRegistry.add(provider_cls)
    return provider_cls
