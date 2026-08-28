"""Tests for the retrieval-pipeline taxonomy (types + protocol contracts)."""

from __future__ import annotations

import json

from collections.abc import Mapping

from scorekeeper.core.retrieved_context import Chunk, RetrievedDocument
from scorekeeper.core.retrieval import (
    STATUS_EN_RECUPERACION,
    AuthDecision,
    AuthProvider,
    AuthRequirement,
    AuthStatus,
    ContentExtractor,
    DocType,
    DocumentFetcher,
    DocumentLocator,
    DocumentLocatorResolver,
    ExtractedContent,
    FetchedDocument,
    RetrievalOutcome,
    RetrievalPipeline,
    RetrievalReport,
    RetrievalStatus,
    RetrievalSummary,
    SourceFormat,
    SourceRef,
    SourceRefParser,
)


# --- Enums --------------------------------------------------------------------


def test_enum_members() -> None:
    assert {f.value for f in SourceFormat} == {
        "pipe_labelled",
        "json_name_url",
        "json_indexed",
        "plaintext",
        "empty",
    }
    assert {d.value for d in DocType} == {"pdf", "docx", "html", "unknown"}
    assert {a.value for a in AuthRequirement} == {"public", "required"}
    assert {a.value for a in AuthStatus} == {
        "not_needed",
        "satisfied",
        "missing_credentials",
    }
    assert {s.value for s in RetrievalStatus} == {
        "pending",
        "retrieved",
        "auth_missing",
        "fetch_failed",
        "unsupported_type",
        "unsupported_scheme",
        "locator_not_found",
        "empty_content",
        "parse_error",
    }


def test_enums_are_str_and_json_serializable() -> None:
    # StrEnum members are plain strings, so they serialize without a custom encoder.
    assert AuthStatus.SATISFIED == "satisfied"
    assert json.dumps({"status": RetrievalStatus.RETRIEVED}) == '{"status": "retrieved"}'


def test_run_status_constant() -> None:
    assert STATUS_EN_RECUPERACION == "en_recuperacion"


# --- Models -------------------------------------------------------------------


def test_source_ref_requires_rank_and_defaults_index() -> None:
    ref = SourceRef(name="host", url="https://x/y.pdf#page=3", rank=0)
    assert ref.index is None
    assert ref.rank == 0


def test_locator_defaults() -> None:
    loc = DocumentLocator(
        document_url="https://x/y.pdf",
        filename="y.pdf",
        doc_type=DocType.PDF,
        host="x",
    )
    assert loc.page is None and loc.section is None


def test_outcome_defaults_to_pending() -> None:
    outcome = RetrievalOutcome(source=SourceRef(name="n", url="u", rank=0))
    assert outcome.status is RetrievalStatus.PENDING
    assert outcome.document is None and outcome.error is None


def test_fetched_document_holds_bytes() -> None:
    fetched = FetchedDocument(
        document_url="https://x/y.pdf", doc_type=DocType.PDF, body=b"%PDF-1.7"
    )
    assert fetched.body == b"%PDF-1.7"
    assert fetched.content_type is None


def test_extracted_content() -> None:
    extracted = ExtractedContent(text="hola", page=3)
    assert extracted.text == "hola" and extracted.page == 3


# --- RetrievalReport.to_context ----------------------------------------------


def _retrieved(rank: int, content: str) -> RetrievalOutcome:
    return RetrievalOutcome(
        source=SourceRef(name=f"n{rank}", url=f"u{rank}", rank=rank),
        status=RetrievalStatus.RETRIEVED,
        document=RetrievedDocument(
            name=f"n{rank}",
            document=f"doc{rank}.pdf",
            url=f"u{rank}",
            chunks=[Chunk(index=0, text=content)],
        ),
    )


def test_to_context_keeps_order_and_skips_non_retrieved() -> None:
    report = RetrievalReport(
        source_format=SourceFormat.JSON_NAME_URL,
        outcomes=[
            _retrieved(0, "uno"),
            RetrievalOutcome(  # skipped: auth missing, no document
                source=SourceRef(name="x", url="ux", rank=1),
                status=RetrievalStatus.AUTH_MISSING,
            ),
            _retrieved(2, "tres"),
        ],
    )
    context = report.to_context()
    assert [d.content for d in context.documents] == ["uno", "tres"]
    # A valid RetrievedContext (round-trips through the storage schema).
    assert context.model_dump()["documents"][0]["document"] == "doc0.pdf"


def test_to_context_empty_when_nothing_retrieved() -> None:
    report = RetrievalReport(
        source_format=SourceFormat.EMPTY,
        outcomes=[
            RetrievalOutcome(
                source=SourceRef(name="x", url="u", rank=0),
                status=RetrievalStatus.FETCH_FAILED,
            )
        ],
    )
    assert report.to_context().is_empty


# --- RetrievalSummary.from_outcomes ------------------------------------------


def test_summary_counts_by_status() -> None:
    outcomes = [
        _retrieved(0, "a"),
        _retrieved(1, "b"),
        RetrievalOutcome(source=SourceRef(name="a", url="u", rank=2), status=RetrievalStatus.AUTH_MISSING),
        RetrievalOutcome(source=SourceRef(name="b", url="u", rank=3), status=RetrievalStatus.FETCH_FAILED),
        RetrievalOutcome(source=SourceRef(name="c", url="u", rank=4), status=RetrievalStatus.UNSUPPORTED_TYPE),
        RetrievalOutcome(source=SourceRef(name="d", url="u", rank=5), status=RetrievalStatus.PARSE_ERROR),
        RetrievalOutcome(source=SourceRef(name="e", url="u", rank=6)),  # PENDING -> other
        RetrievalOutcome(
            source=SourceRef(name="f", url="mailto:x@y.z", rank=7),
            status=RetrievalStatus.UNSUPPORTED_SCHEME,
        ),
    ]
    summary = RetrievalSummary.from_outcomes(outcomes)
    assert summary.total == 8
    assert summary.retrieved == 2
    assert summary.auth_missing == 1
    assert summary.fetch_failed == 1
    assert summary.unsupported == 1  # UNSUPPORTED_TYPE only
    assert summary.other == 3  # PARSE_ERROR + PENDING + UNSUPPORTED_SCHEME


def test_summary_empty() -> None:
    summary = RetrievalSummary.from_outcomes([])
    assert summary.total == 0 and summary.retrieved == 0


# --- Protocol conformance -----------------------------------------------------


class _DummyPipeline:
    def run(self, cell: str) -> RetrievalReport:
        return RetrievalReport(source_format=SourceFormat.EMPTY)

    def purge_cache(self) -> int:
        return 0


class _DummyAuthProvider:
    def classify(self, locator: DocumentLocator) -> AuthDecision:
        return AuthDecision(
            requirement=AuthRequirement.PUBLIC, status=AuthStatus.NOT_NEEDED
        )

    def headers(self, locator: DocumentLocator) -> Mapping[str, str]:
        return {}

    def client(self, locator: DocumentLocator):
        return None


class _DummyParser:
    def detect(self, cell: str) -> SourceFormat:
        return SourceFormat.EMPTY

    def parse(self, cell: str) -> list[SourceRef]:
        return []


class _DummyResolver:
    def resolve(self, source: SourceRef) -> DocumentLocator:
        return DocumentLocator(
            document_url=source.url, filename="f", doc_type=DocType.UNKNOWN, host="h"
        )


class _DummyFetcher:
    def fetch(self, locator: DocumentLocator, client: object | None) -> FetchedDocument:
        return FetchedDocument(document_url=locator.document_url, doc_type=locator.doc_type, body=b"")

    def purge_cache(self) -> int:
        return 0


class _DummyExtractor:
    def supports(self, doc_type: DocType) -> bool:
        return True

    def extract(self, document: FetchedDocument, locator: DocumentLocator) -> ExtractedContent:
        return ExtractedContent(text="")


def test_dummies_conform_to_protocols() -> None:
    assert isinstance(_DummyPipeline(), RetrievalPipeline)
    assert isinstance(_DummyAuthProvider(), AuthProvider)
    assert isinstance(_DummyParser(), SourceRefParser)
    assert isinstance(_DummyResolver(), DocumentLocatorResolver)
    assert isinstance(_DummyFetcher(), DocumentFetcher)
    assert isinstance(_DummyExtractor(), ContentExtractor)


def test_non_conforming_object_fails_protocol_check() -> None:
    class NotAPipeline:
        pass

    assert not isinstance(NotAPipeline(), RetrievalPipeline)
    assert not isinstance(object(), SourceRefParser)
