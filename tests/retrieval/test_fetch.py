"""Tests for the caching document fetcher (fetch stage)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from scorekeeper.database import Base, DocumentCacheEntry
from scorekeeper.retrieval import CachingDocumentFetcher, DocumentFetcher, FetchError
from scorekeeper.retrieval.types import DocType, DocumentLocator


class _Resp:
    def __init__(self, *, content: bytes = b"", headers: dict | None = None, status: int = 200):
        self.content = content
        self.headers = headers or {}
        self._status = status

    def raise_for_status(self) -> None:
        if self._status >= 400:
            raise httpx2.HTTPError(f"HTTP {self._status}")


class _FakeHttp:
    """Records GET calls; returns fixed bytes so we can assert on download count."""

    def __init__(self, *, content: bytes = b"PDF-BYTES", content_type: str = "application/pdf"):
        self._content = content
        self._content_type = content_type
        self.calls = 0

    def get(self, url: str) -> _Resp:
        self.calls += 1
        return _Resp(content=self._content, headers={"content-type": self._content_type})


class _FakeClient:
    """A generic AuthClient whose download() returns fixed bytes."""

    kind = "sharepoint"

    def __init__(self) -> None:
        self.calls = 0

    def download(self, locator: DocumentLocator) -> bytes:
        self.calls += 1
        return b"AUTHED-BYTES"


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _fetcher(tmp_path: Path, session: Session, http: _FakeHttp | None = None) -> CachingDocumentFetcher:
    return CachingDocumentFetcher(
        cache_dir=tmp_path, session=session, http_client=http or _FakeHttp()
    )


def _loc(url: str, doc_type: DocType = DocType.PDF) -> DocumentLocator:
    return DocumentLocator(document_url=url, filename="x.pdf", doc_type=doc_type, host="h")


def test_conforms_to_protocol(tmp_path: Path, session: Session) -> None:
    assert isinstance(_fetcher(tmp_path, session), DocumentFetcher)


def test_public_fetch_downloads_and_caches(tmp_path: Path, session: Session) -> None:
    http = _FakeHttp()
    fetched = _fetcher(tmp_path, session, http).fetch(_loc("https://pub/x.pdf"), None)
    assert fetched.body == b"PDF-BYTES"
    assert fetched.content_type == "application/pdf"
    assert fetched.cached is False
    assert http.calls == 1
    assert session.query(DocumentCacheEntry).count() == 1


def test_second_fetch_hits_cache_without_redownloading(tmp_path: Path, session: Session) -> None:
    http = _FakeHttp()
    fetcher = _fetcher(tmp_path, session, http)
    first = fetcher.fetch(_loc("https://pub/x.pdf"), None)
    second = fetcher.fetch(_loc("https://pub/x.pdf"), None)
    assert first.cached is False
    assert second.cached is True
    assert second.body == b"PDF-BYTES"
    assert http.calls == 1  # served from disk the second time


def test_duplicate_references_yield_duplicate_results(tmp_path: Path, session: Session) -> None:
    # The same URL referenced twice in a cell must produce two results (duplicates allowed);
    # only the first touches the network.
    http = _FakeHttp()
    fetcher = _fetcher(tmp_path, session, http)
    results = [fetcher.fetch(_loc("https://pub/dup.pdf"), None) for _ in range(3)]
    assert len(results) == 3
    assert all(r.body == b"PDF-BYTES" for r in results)
    assert [r.cached for r in results] == [False, True, True]
    assert http.calls == 1
    assert session.query(DocumentCacheEntry).count() == 1


def test_client_based_fetch_uses_download(tmp_path: Path, session: Session) -> None:
    client = _FakeClient()
    fetcher = _fetcher(tmp_path, session)
    fetched = fetcher.fetch(_loc("https://cognitactix-my.sharepoint.com/y.pdf"), client)
    assert fetched.body == b"AUTHED-BYTES"
    assert fetched.content_type is None  # the generic client surfaces no Content-Type
    assert client.calls == 1
    # Cached like any other fetch: a repeat does not call the client again.
    again = fetcher.fetch(_loc("https://cognitactix-my.sharepoint.com/y.pdf"), client)
    assert again.cached is True
    assert client.calls == 1


def test_fetch_failure_raises_fetch_error(tmp_path: Path, session: Session) -> None:
    class _BadHttp:
        def get(self, url: str) -> _Resp:
            raise httpx2.HTTPError("boom")

    fetcher = CachingDocumentFetcher(cache_dir=tmp_path, session=session, http_client=_BadHttp())
    with pytest.raises(FetchError):
        fetcher.fetch(_loc("https://pub/fail.pdf"), None)
    # Nothing cached on failure.
    assert session.query(DocumentCacheEntry).count() == 0


def test_distinct_urls_download_separately(tmp_path: Path, session: Session) -> None:
    http = _FakeHttp()
    fetcher = _fetcher(tmp_path, session, http)
    fetcher.fetch(_loc("https://pub/a.pdf"), None)
    fetcher.fetch(_loc("https://pub/b.pdf"), None)
    assert http.calls == 2
    assert session.query(DocumentCacheEntry).count() == 2
