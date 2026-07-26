"""Tests for the Locate stage (``UrlDocumentLocatorResolver``)."""

from __future__ import annotations

from scorekeeper.core.retrieval import (
    DocType,
    DocumentLocatorResolver,
    SourceRef,
    UrlDocumentLocatorResolver,
)


def _ref(url: str) -> SourceRef:
    return SourceRef(name="host", url=url, rank=0)


def test_resolve_pdf_with_page_fragment() -> None:
    resolver = UrlDocumentLocatorResolver()
    locator = resolver.resolve(
        _ref("https://Cognitactix-my.SharePoint.com/sites/x/ReporteFinanciero.pdf#page=3")
    )
    assert locator.document_url == (
        "https://Cognitactix-my.SharePoint.com/sites/x/ReporteFinanciero.pdf"
    )
    assert locator.filename == "ReporteFinanciero.pdf"
    assert locator.doc_type is DocType.PDF
    assert locator.host == "cognitactix-my.sharepoint.com"  # lowercased
    assert locator.page == 3
    assert locator.section is None


def test_resolve_html_extensions() -> None:
    resolver = UrlDocumentLocatorResolver()
    assert resolver.resolve(_ref("https://eleconomista.com.mx/a.html")).doc_type is DocType.HTML
    assert resolver.resolve(_ref("https://eleconomista.com.mx/a.htm")).doc_type is DocType.HTML


def test_resolve_docx_and_legacy_doc() -> None:
    resolver = UrlDocumentLocatorResolver()
    # .docx maps to WORD; legacy binary .doc is intentionally unmapped (unsupported).
    assert resolver.resolve(_ref("https://sp/sites/x/Reporte.docx")).doc_type is DocType.DOCX
    assert resolver.resolve(_ref("https://sp/sites/x/Reporte.doc")).doc_type is DocType.UNKNOWN


def test_resolve_unquotes_encoded_filename() -> None:
    resolver = UrlDocumentLocatorResolver()
    locator = resolver.resolve(_ref("https://sp/sites/x/Reporte%20Financiero%202024.pdf"))
    assert locator.filename == "Reporte Financiero 2024.pdf"
    assert locator.doc_type is DocType.PDF


def test_resolve_keeps_query_in_url_but_not_filename() -> None:
    resolver = UrlDocumentLocatorResolver()
    locator = resolver.resolve(_ref("https://x/a.pdf?token=abc&v=2#page=5"))
    assert locator.document_url == "https://x/a.pdf?token=abc&v=2"  # fragment gone, query kept
    assert locator.filename == "a.pdf"  # query ignored
    assert locator.page == 5


def test_resolve_non_page_fragment_becomes_section() -> None:
    resolver = UrlDocumentLocatorResolver()
    locator = resolver.resolve(_ref("https://x/a.html#seccion-2"))
    assert locator.page is None
    assert locator.section == "seccion-2"


def test_resolve_extensionless_trailing_slash() -> None:
    resolver = UrlDocumentLocatorResolver()
    locator = resolver.resolve(_ref("https://eleconomista.com.mx/noticias/"))
    assert locator.filename == ""
    assert locator.doc_type is DocType.UNKNOWN
    assert locator.page is None
    assert locator.section is None


def test_resolve_extension_is_case_insensitive() -> None:
    resolver = UrlDocumentLocatorResolver()
    assert resolver.resolve(_ref("https://x/A.PDF")).doc_type is DocType.PDF


def test_resolver_conforms_to_protocol() -> None:
    assert isinstance(UrlDocumentLocatorResolver(), DocumentLocatorResolver)
