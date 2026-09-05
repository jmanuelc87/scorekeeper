"""Tests for the caching document fetcher (fetch stage)."""

from __future__ import annotations

from pathlib import Path

import httpx2
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.db.models import DocumentCacheEntry
from scorekeeper.core.retrieval import CachingDocumentFetcher, DocumentFetcher, FetchError
from scorekeeper.core.retrieval import fetch
from scorekeeper.core.retrieval.types import DocType, DocumentLocator


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch) -> None:
    """Resolve every host to a public address, so the target guard allows the fake URLs.

    Tests that exercise the guard itself patch this again with what they need.
    """
    monkeypatch.setattr(fetch, "_resolve_addresses", lambda host: ["93.184.216.34"])


class _Resp:
    def __init__(
        self,
        *,
        content: bytes = b"",
        headers: dict | None = None,
        status: int = 200,
        location: str | None = None,
    ):
        self.content = content
        self.headers = dict(headers or {})
        self._status = status
        self.is_redirect = location is not None
        if location is not None:
            self.headers["location"] = location

    def raise_for_status(self) -> None:
        if self._status >= 400:
            raise httpx2.HTTPError(f"HTTP {self._status}")


class _FakeHttp:
    """Records GET calls; returns fixed bytes so we can assert on download count."""

    def __init__(self, *, content: bytes = b"PDF-BYTES", content_type: str = "application/pdf"):
        self._content = content
        self._content_type = content_type
        self.calls = 0
        self.urls: list[str] = []

    def get(self, url: str) -> _Resp:
        self.calls += 1
        self.urls.append(url)
        headers = {"content-type": self._content_type} if self._content_type else {}
        return _Resp(content=self._content, headers=headers)


class _FakeClient:
    """A generic AuthClient whose download() returns fixed bytes."""

    kind = "sharepoint"

    def __init__(self) -> None:
        self.calls = 0

    def download(self, locator: DocumentLocator) -> bytes:
        self.calls += 1
        return b"AUTHED-BYTES"


def _fetcher(tmp_path: Path, session: AsyncSession, http: _FakeHttp | None = None) -> CachingDocumentFetcher:
    return CachingDocumentFetcher(
        cache_dir=tmp_path, session=session, http_client=http or _FakeHttp()
    )


def _loc(url: str, doc_type: DocType = DocType.PDF) -> DocumentLocator:
    return DocumentLocator(document_url=url, filename="x.pdf", doc_type=doc_type, host="h")


async def test_conforms_to_protocol(tmp_path: Path, session: AsyncSession) -> None:
    assert isinstance(_fetcher(tmp_path, session), DocumentFetcher)


async def test_public_fetch_downloads_and_caches(tmp_path: Path, session: AsyncSession) -> None:
    http = _FakeHttp()
    fetched = await _fetcher(tmp_path, session, http).fetch(_loc("https://pub/x.pdf"), None)
    assert fetched.body == b"PDF-BYTES"
    assert fetched.content_type == "application/pdf"
    assert fetched.cached is False
    assert http.calls == 1
    assert await session.scalar(select(func.count()).select_from(DocumentCacheEntry)) == 1


async def test_second_fetch_hits_cache_without_redownloading(tmp_path: Path, session: AsyncSession) -> None:
    http = _FakeHttp()
    fetcher = _fetcher(tmp_path, session, http)
    first = await fetcher.fetch(_loc("https://pub/x.pdf"), None)
    second = await fetcher.fetch(_loc("https://pub/x.pdf"), None)
    assert first.cached is False
    assert second.cached is True
    assert second.body == b"PDF-BYTES"
    assert http.calls == 1  # served from disk the second time


async def test_duplicate_references_yield_duplicate_results(tmp_path: Path, session: AsyncSession) -> None:
    # The same URL referenced twice in a cell must produce two results (duplicates allowed);
    # only the first touches the network.
    http = _FakeHttp()
    fetcher = _fetcher(tmp_path, session, http)
    results = [await fetcher.fetch(_loc("https://pub/dup.pdf"), None) for _ in range(3)]
    assert len(results) == 3
    assert all(r.body == b"PDF-BYTES" for r in results)
    assert [r.cached for r in results] == [False, True, True]
    assert http.calls == 1
    assert await session.scalar(select(func.count()).select_from(DocumentCacheEntry)) == 1


async def test_client_based_fetch_uses_download(tmp_path: Path, session: AsyncSession) -> None:
    client = _FakeClient()
    fetcher = _fetcher(tmp_path, session)
    fetched = await fetcher.fetch(_loc("https://cognitactix-my.sharepoint.com/y.pdf"), client)
    assert fetched.body == b"AUTHED-BYTES"
    assert fetched.content_type is None  # the generic client surfaces no Content-Type
    assert client.calls == 1
    # Cached like any other fetch: a repeat does not call the client again.
    again = await fetcher.fetch(_loc("https://cognitactix-my.sharepoint.com/y.pdf"), client)
    assert again.cached is True
    assert client.calls == 1


async def test_fetch_failure_raises_fetch_error(tmp_path: Path, session: AsyncSession) -> None:
    class _BadHttp:
        def get(self, url: str) -> _Resp:
            raise httpx2.HTTPError("boom")

    fetcher = CachingDocumentFetcher(cache_dir=tmp_path, session=session, http_client=_BadHttp())
    with pytest.raises(FetchError):
        await fetcher.fetch(_loc("https://pub/fail.pdf"), None)
    # Nothing cached on failure.
    assert await session.scalar(select(func.count()).select_from(DocumentCacheEntry)) == 0


# -- document type confirmation ------------------------------------------------


async def test_content_type_confirms_the_provisional_type(tmp_path: Path, session: AsyncSession) -> None:
    # An extensionless page URL arrives as a provisional HTML; the response says PDF, and
    # the response wins — for the returned document and for what the cache replays.
    http = _FakeHttp(content_type="application/pdf; charset=binary")
    fetcher = _fetcher(tmp_path, session, http)
    locator = _loc("https://pub/descarga", DocType.HTML)

    fetched = await fetcher.fetch(locator, None)
    assert fetched.doc_type is DocType.PDF

    assert (await fetcher.fetch(locator, None)).doc_type is DocType.PDF  # from the cache


async def test_unknown_or_absent_content_type_keeps_the_locator_type(tmp_path: Path, session: AsyncSession) -> None:
    absent = await _fetcher(tmp_path, session, _FakeHttp(content_type="")).fetch(
        _loc("https://pub/sin-cabecera", DocType.HTML), None
    )
    assert absent.doc_type is DocType.HTML

    unmapped = await _fetcher(tmp_path, session, _FakeHttp(content_type="text/plain")).fetch(
        _loc("https://pub/otra", DocType.HTML), None
    )
    assert unmapped.doc_type is DocType.HTML


@pytest.mark.parametrize("content_type", ["image/svg+xml", "image/png", "IMAGE/JPEG"])
async def test_an_image_response_is_not_treated_as_a_web_page(
    content_type: str, tmp_path: Path, session: AsyncSession
) -> None:
    """An extensionless chart endpoint is provisionally HTML; its bytes are not a page.

    Converting an SVG as HTML would mine it for stray ``<text>`` fragments and store that
    as retrieved context, so the type is downgraded and the reference ends UNSUPPORTED_TYPE.
    """
    fetched = await _fetcher(tmp_path, session, _FakeHttp(content_type=content_type)).fetch(
        _loc("https://pub/grafico", DocType.HTML), None
    )
    assert fetched.doc_type is DocType.UNKNOWN


async def test_html_response_overrides_a_pdf_url(tmp_path: Path, session: AsyncSession) -> None:
    # A .pdf URL that actually serves an HTML error/login page converts as HTML.
    http = _FakeHttp(content=b"<html>error</html>", content_type="text/html; charset=utf-8")
    fetched = await _fetcher(tmp_path, session, http).fetch(_loc("https://pub/x.pdf"), None)
    assert fetched.doc_type is DocType.HTML


async def test_authenticated_download_keeps_the_locator_type(tmp_path: Path, session: AsyncSession) -> None:
    fetched = await _fetcher(tmp_path, session).fetch(_loc("https://sp/y.pdf"), _FakeClient())
    assert fetched.doc_type is DocType.PDF  # no Content-Type to confirm with


# -- redirects and target restriction -------------------------------------------


async def test_redirects_are_followed_hop_by_hop(tmp_path: Path, session: AsyncSession) -> None:
    class _RedirectingHttp(_FakeHttp):
        def get(self, url: str) -> _Resp:
            self.calls += 1
            self.urls.append(url)
            if url.endswith("/inicio"):
                return _Resp(location="/final.pdf")  # relative, resolved against the hop
            return _Resp(content=b"PDF-BYTES", headers={"content-type": "application/pdf"})

    http = _RedirectingHttp()
    fetched = await _fetcher(tmp_path, session, http).fetch(_loc("https://pub/inicio"), None)

    assert fetched.body == b"PDF-BYTES"
    assert http.urls == ["https://pub/inicio", "https://pub/final.pdf"]


async def test_redirect_loop_gives_up(tmp_path: Path, session: AsyncSession) -> None:
    class _LoopingHttp(_FakeHttp):
        def get(self, url: str) -> _Resp:
            self.calls += 1
            return _Resp(location="https://pub/siguiente")

    with pytest.raises(FetchError, match="redirecciones"):
        await _fetcher(tmp_path, session, _LoopingHttp()).fetch(_loc("https://pub/bucle"), None)


async def test_private_target_is_refused(tmp_path: Path, session: AsyncSession, monkeypatch) -> None:
    monkeypatch.setattr(fetch, "_resolve_addresses", lambda host: ["127.0.0.1"])
    http = _FakeHttp()

    with pytest.raises(FetchError, match="red interna"):
        await _fetcher(tmp_path, session, http).fetch(_loc("http://localhost/x.pdf"), None)

    assert http.calls == 0  # refused before the request went out


async def test_redirect_to_a_private_target_is_refused(tmp_path: Path, session: AsyncSession, monkeypatch) -> None:
    # The hop that matters is the one the guard would miss if the client followed redirects.
    monkeypatch.setattr(
        fetch,
        "_resolve_addresses",
        lambda host: ["169.254.169.254"] if host == "metadata" else ["93.184.216.34"],
    )

    class _RedirectingHttp(_FakeHttp):
        def get(self, url: str) -> _Resp:
            self.calls += 1
            return _Resp(location="http://metadata/latest/meta-data/")

    http = _RedirectingHttp()
    with pytest.raises(FetchError, match="red interna"):
        await _fetcher(tmp_path, session, http).fetch(_loc("https://pub/redirige"), None)

    assert http.calls == 1  # the first hop happened, the internal one did not


async def test_redirect_to_a_non_web_scheme_is_refused(tmp_path: Path, session: AsyncSession) -> None:
    class _RedirectingHttp(_FakeHttp):
        def get(self, url: str) -> _Resp:
            self.calls += 1
            return _Resp(location="file:///etc/passwd")

    with pytest.raises(FetchError, match="esquema no soportado"):
        await _fetcher(tmp_path, session, _RedirectingHttp()).fetch(_loc("https://pub/r"), None)


async def test_unresolvable_host_is_a_fetch_error(tmp_path: Path, session: AsyncSession, monkeypatch) -> None:
    def _boom(host: str) -> list[str]:
        raise OSError("Name or service not known")

    monkeypatch.setattr(fetch, "_resolve_addresses", _boom)
    with pytest.raises(FetchError):
        await _fetcher(tmp_path, session).fetch(_loc("https://no-existe/x.pdf"), None)


async def test_distinct_urls_download_separately(tmp_path: Path, session: AsyncSession) -> None:
    http = _FakeHttp()
    fetcher = _fetcher(tmp_path, session, http)
    await fetcher.fetch(_loc("https://pub/a.pdf"), None)
    await fetcher.fetch(_loc("https://pub/b.pdf"), None)
    assert http.calls == 2
    assert await session.scalar(select(func.count()).select_from(DocumentCacheEntry)) == 2


# -- purge_cache ---------------------------------------------------------------


async def test_purge_cache_removes_what_was_fetched(tmp_path: Path, session: AsyncSession) -> None:
    http = _FakeHttp()
    fetcher = _fetcher(tmp_path, session, http)
    await fetcher.fetch(_loc("https://pub/a.pdf"), None)
    await fetcher.fetch(_loc("https://pub/b.pdf"), None)

    assert await fetcher.purge_cache() == 2

    assert await session.scalar(select(func.count()).select_from(DocumentCacheEntry)) == 0
    assert list(tmp_path.rglob("*.pdf")) == []  # no blobs left on disk


async def test_purge_cache_covers_documents_served_from_cache(tmp_path: Path, session: AsyncSession) -> None:
    # A second fetcher that only ever hit the cache still owns what it served: the URL was
    # used by this execution, so it must not survive it.
    await _fetcher(tmp_path, session).fetch(_loc("https://pub/x.pdf"), None)
    second = _fetcher(tmp_path, session)
    assert (await second.fetch(_loc("https://pub/x.pdf"), None)).cached is True

    assert await second.purge_cache() == 1
    assert await session.scalar(select(func.count()).select_from(DocumentCacheEntry)) == 0


async def test_purge_cache_leaves_other_fetchers_documents(tmp_path: Path, session: AsyncSession) -> None:
    mine = _fetcher(tmp_path, session)
    await mine.fetch(_loc("https://pub/mine.pdf"), None)
    theirs = _fetcher(tmp_path, session)
    await theirs.fetch(_loc("https://pub/theirs.pdf"), None)

    assert await mine.purge_cache() == 1

    remaining = (await session.execute(select(DocumentCacheEntry))).scalars().one()
    assert remaining.url == "https://pub/theirs.pdf"


async def test_purge_cache_is_idempotent_and_empty_is_a_no_op(tmp_path: Path, session: AsyncSession) -> None:
    fetcher = _fetcher(tmp_path, session)
    assert await fetcher.purge_cache() == 0  # nothing fetched yet
    await fetcher.fetch(_loc("https://pub/x.pdf"), None)
    assert await fetcher.purge_cache() == 1
    assert await fetcher.purge_cache() == 0  # the served set was cleared, not re-purged


async def test_fetch_after_purge_downloads_again(tmp_path: Path, session: AsyncSession) -> None:
    http = _FakeHttp()
    fetcher = _fetcher(tmp_path, session, http)
    await fetcher.fetch(_loc("https://pub/x.pdf"), None)
    await fetcher.purge_cache()

    again = await fetcher.fetch(_loc("https://pub/x.pdf"), None)

    assert again.cached is False  # the cache no longer holds it
    assert again.body == b"PDF-BYTES"
    assert http.calls == 2


async def test_failed_fetch_purges_cleanly(tmp_path: Path, session: AsyncSession) -> None:
    class _BadHttp:
        def get(self, url: str) -> _Resp:
            raise httpx2.HTTPError("boom")

    fetcher = CachingDocumentFetcher(cache_dir=tmp_path, session=session, http_client=_BadHttp())
    with pytest.raises(FetchError):
        await fetcher.fetch(_loc("https://pub/fail.pdf"), None)
    assert await fetcher.purge_cache() == 0  # nothing was cached, so nothing to remove
