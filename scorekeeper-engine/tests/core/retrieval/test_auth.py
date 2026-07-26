"""Tests for the Authorize stage (``HostRuleAuthProvider``)."""

from __future__ import annotations

from scorekeeper.core.retrieval import (
    AuthProvider,
    AuthRequirement,
    AuthStatus,
    DocType,
    DocumentLocator,
    HostRuleAuthProvider,
)


def _locator(host: str) -> DocumentLocator:
    return DocumentLocator(
        document_url=f"https://{host}/x/a.pdf",
        filename="a.pdf",
        doc_type=DocType.PDF,
        host=host,
    )


def test_public_host_is_not_needed() -> None:
    provider = HostRuleAuthProvider()
    decision = provider.classify(_locator("eleconomista.com.mx"))
    assert decision.requirement is AuthRequirement.PUBLIC
    assert decision.status is AuthStatus.NOT_NEEDED
    assert decision.provider is None
    assert provider.headers(_locator("eleconomista.com.mx")) == {}


def test_gated_host_without_credentials_is_missing() -> None:
    provider = HostRuleAuthProvider()  # no credentials wired in
    decision = provider.classify(_locator("cognitactix-my.sharepoint.com"))
    assert decision.requirement is AuthRequirement.REQUIRED
    assert decision.status is AuthStatus.MISSING_CREDENTIALS
    assert decision.provider == "host-rule"
    assert provider.headers(_locator("cognitactix-my.sharepoint.com")) == {}


def test_gated_host_with_credential_is_satisfied() -> None:
    provider = HostRuleAuthProvider(
        credentials={"cognitactix-my.sharepoint.com": "tok-123"}
    )
    locator = _locator("cognitactix-my.sharepoint.com")
    decision = provider.classify(locator)
    assert decision.requirement is AuthRequirement.REQUIRED
    assert decision.status is AuthStatus.SATISFIED
    assert provider.headers(locator) == {"Authorization": "Bearer tok-123"}


def test_subdomain_matches_gated_domain() -> None:
    provider = HostRuleAuthProvider()
    # Both the apex and a subdomain of a gated domain are REQUIRED.
    assert provider.classify(_locator("sharepoint.com")).requirement is AuthRequirement.REQUIRED
    assert (
        provider.classify(_locator("a.b.sharepoint.com")).requirement
        is AuthRequirement.REQUIRED
    )
    # A domain that merely ends with the same text but is not a subdomain is not gated.
    assert (
        provider.classify(_locator("notsharepoint.com")).requirement
        is AuthRequirement.PUBLIC
    )


def test_host_matching_is_case_insensitive() -> None:
    provider = HostRuleAuthProvider(credentials={"cognitactix-my.sharepoint.com": "tok"})
    locator = _locator("Cognitactix-My.SharePoint.com")
    assert provider.classify(locator).status is AuthStatus.SATISFIED
    assert provider.headers(locator) == {"Authorization": "Bearer tok"}


def test_custom_gated_domains() -> None:
    provider = HostRuleAuthProvider(gated_domains=["intranet.example.com"])
    # SharePoint is no longer gated once the domain set is overridden.
    assert (
        provider.classify(_locator("x.sharepoint.com")).requirement is AuthRequirement.PUBLIC
    )
    assert (
        provider.classify(_locator("docs.intranet.example.com")).requirement
        is AuthRequirement.REQUIRED
    )


def test_credential_for_non_gated_host_is_ignored() -> None:
    # A credential held for a PUBLIC host never turns it into an auth'd fetch.
    provider = HostRuleAuthProvider(credentials={"eleconomista.com.mx": "tok"})
    locator = _locator("eleconomista.com.mx")
    assert provider.classify(locator).status is AuthStatus.NOT_NEEDED
    assert provider.headers(locator) == {}


def test_provider_conforms_to_protocol() -> None:
    assert isinstance(HostRuleAuthProvider(), AuthProvider)
