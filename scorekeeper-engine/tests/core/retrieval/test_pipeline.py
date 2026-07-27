"""Tests for the retrieval orchestrator (RetrievalOrchestrator) with injected fake stages."""

from __future__ import annotations

from scorekeeper.core.retrieval import (
    RetrievalOrchestrator,
    RetrievalPipeline,
    UrlDocumentLocatorResolver,
)
from scorekeeper.core.retrieval.credentials import CredentialError
from scorekeeper.core.retrieval.extract import (
    ExtractError,
    MarkdownContentExtractor,
    PageNotFoundError,
)
from scorekeeper.core.retrieval.fetch import FetchError
from scorekeeper.core.retrieval.types import (
    AuthDecision,
    AuthRequirement,
    AuthStatus,
    DocType,
    DocumentLocator,
    ExtractedContent,
    FetchedDocument,
    RetrievalStatus,
    SourceFormat,
    SourceRef,
)


class _Parser:
    def __init__(self, refs, *, raise_exc=None, fmt=SourceFormat.JSON_INDEXED):
        self._refs = refs
        self._raise = raise_exc
        self._fmt = fmt

    def detect(self, cell):
        return self._fmt

    def parse(self, cell):
        if self._raise is not None:
            raise self._raise
        return self._refs


class _Resolver:
    def __init__(self, doc_type=DocType.PDF, scheme="https"):
        self._doc_type = doc_type
        self._scheme = scheme

    def resolve(self, source):
        return DocumentLocator(
            document_url=source.url.split("#")[0],
            filename="f.pdf",
            doc_type=self._doc_type,
            scheme=self._scheme,
            host="h",
            page=1,
        )


class _Auth:
    def __init__(self, *, status=AuthStatus.NOT_NEEDED, client=None, client_exc=None):
        self._status = status
        self._client = client
        self._client_exc = client_exc

    async def classify(self, locator):
        requirement = (
            AuthRequirement.PUBLIC
            if self._status is AuthStatus.NOT_NEEDED
            else AuthRequirement.REQUIRED
        )
        return AuthDecision(requirement=requirement, status=self._status, provider="fake")

    def headers(self, locator):
        return {}

    async def client(self, locator):
        if self._client_exc is not None:
            raise self._client_exc
        return self._client


class _Fetcher:
    def __init__(self, *, body=b"bytes", exc=None):
        self._body = body
        self._exc = exc
        self.purges = 0

    async def fetch(self, locator, client):
        if self._exc is not None:
            raise self._exc
        return FetchedDocument(
            document_url=locator.document_url, doc_type=locator.doc_type, body=self._body
        )

    async def purge_cache(self):
        self.purges += 1
        return 2


class _Extractor:
    def __init__(self, *, text="# markdown", supported=True, exc=None):
        self._text = text
        self._supported = supported
        self._exc = exc

    def supports(self, doc_type):
        return self._supported

    def extract(self, document, locator):
        if self._exc is not None:
            raise self._exc
        return ExtractedContent(text=self._text, page=locator.page)


def _ref(rank=0, *, name="A", url="https://h/a.pdf#page=1"):
    return SourceRef(name=name, url=url, rank=rank)


def _orch(*, refs=None, parser=None, resolver=None, auth=None, fetcher=None, extractor=None):
    return RetrievalOrchestrator(
        parser=parser or _Parser(refs if refs is not None else [_ref()]),
        resolver=resolver or _Resolver(),
        auth_provider=auth or _Auth(),
        fetcher=fetcher or _Fetcher(),
        extractor=extractor or _Extractor(),
    )


async def test_conforms_to_protocol() -> None:
    assert isinstance(_orch(), RetrievalPipeline)


async def test_happy_path_retrieved() -> None:
    report = await _orch().run("cell")
    assert report.source_format is SourceFormat.JSON_INDEXED
    assert [o.status for o in report.outcomes] == [RetrievalStatus.RETRIEVED]
    docs = report.to_context().documents
    assert len(docs) == 1 and docs[0].content == "# markdown"
    assert report.outcomes[0].auth is not None  # authorize result threaded through


async def test_empty_cell_yields_empty_report() -> None:
    report = await _orch(parser=_Parser([], fmt=SourceFormat.EMPTY)).run("")
    assert report.outcomes == []
    assert report.to_context().documents == []


async def test_parser_failure_is_parse_error() -> None:
    report = await _orch(parser=_Parser([], raise_exc=RuntimeError("llm down"))).run("blah")
    assert [o.status for o in report.outcomes] == [RetrievalStatus.PARSE_ERROR]
    assert "llm down" in report.outcomes[0].error


async def test_missing_credentials_is_auth_missing() -> None:
    report = await _orch(auth=_Auth(status=AuthStatus.MISSING_CREDENTIALS)).run("cell")
    assert report.outcomes[0].status is RetrievalStatus.AUTH_MISSING


async def test_unsupported_type() -> None:
    report = await _orch(extractor=_Extractor(supported=False)).run("cell")
    assert report.outcomes[0].status is RetrievalStatus.UNSUPPORTED_TYPE


async def test_non_web_scheme_is_unsupported_scheme() -> None:
    # The guard runs before authorize: an auth stage that would answer MISSING_CREDENTIALS
    # never gets the chance, so the reference reads as a bad URL, not a missing credential.
    report = await _orch(
        resolver=_Resolver(scheme="mailto"), auth=_Auth(status=AuthStatus.MISSING_CREDENTIALS)
    ).run("cell")
    assert report.outcomes[0].status is RetrievalStatus.UNSUPPORTED_SCHEME
    assert "mailto" in report.outcomes[0].error


async def test_reference_without_a_scheme_is_unsupported_scheme() -> None:
    report = await _orch(resolver=_Resolver(scheme="")).run("cell")
    assert report.outcomes[0].status is RetrievalStatus.UNSUPPORTED_SCHEME
    assert "sin esquema" in report.outcomes[0].error


async def test_extensionless_web_page_is_retrieved() -> None:
    # End to end over the real resolver and extractor: a plain article URL carries no
    # extension, and must still reach the fetch stage instead of being ruled unsupported.
    orch = _orch(
        refs=[_ref(url="https://eleconomista.com.mx/noticias/")],
        resolver=UrlDocumentLocatorResolver(),
        extractor=MarkdownContentExtractor(converter=lambda body, ext: "# nota"),
    )
    report = await orch.run("cell")
    assert report.outcomes[0].status is RetrievalStatus.RETRIEVED
    assert report.to_context().documents[0].content == "# nota"


async def test_client_build_failure_is_auth_missing() -> None:
    auth = _Auth(status=AuthStatus.SATISFIED, client_exc=CredentialError("no sdk"))
    report = await _orch(auth=auth).run("cell")
    assert report.outcomes[0].status is RetrievalStatus.AUTH_MISSING


async def test_fetch_error_is_fetch_failed() -> None:
    report = await _orch(fetcher=_Fetcher(exc=FetchError("boom"))).run("cell")
    assert report.outcomes[0].status is RetrievalStatus.FETCH_FAILED


async def test_page_not_found_is_locator_not_found() -> None:
    report = await _orch(extractor=_Extractor(exc=PageNotFoundError("no page 9"))).run("cell")
    assert report.outcomes[0].status is RetrievalStatus.LOCATOR_NOT_FOUND


async def test_extract_error_is_fetch_failed() -> None:
    report = await _orch(extractor=_Extractor(exc=ExtractError("corrupt"))).run("cell")
    assert report.outcomes[0].status is RetrievalStatus.FETCH_FAILED


async def test_empty_extract_is_empty_content() -> None:
    report = await _orch(extractor=_Extractor(text="   ")).run("cell")
    assert report.outcomes[0].status is RetrievalStatus.EMPTY_CONTENT
    assert report.to_context().documents == []  # excluded from the assembled context


async def test_order_and_duplicates_preserved() -> None:
    refs = [_ref(0, name="A"), _ref(1, name="B", url="https://h/b.pdf"), _ref(2, name="A")]
    report = await _orch(refs=refs).run("cell")
    # Three RETRIEVED outcomes, rank order kept, the duplicate ref kept as its own document.
    assert len(report.outcomes) == 3
    docs = report.to_context().documents
    assert [d.name for d in docs] == ["A", "B", "A"]


async def test_purge_cache_delegates_to_the_fetch_stage() -> None:
    fetcher = _Fetcher()
    orch = _orch(fetcher=fetcher)
    await orch.run("cell")
    assert await orch.purge_cache() == 2  # the fetcher's removal count is passed through
    assert fetcher.purges == 1
