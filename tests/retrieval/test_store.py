"""Tests for the filesystem document store (fetch-stage cache)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from scorekeeper.database import Base, DocumentCacheEntry
from scorekeeper.retrieval.store import DocumentStore
from scorekeeper.retrieval.types import DocType


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def store(tmp_path: Path, session: Session) -> DocumentStore:
    return DocumentStore(tmp_path, session=session)


_URL = "https://host/doc.pdf"


def test_put_writes_blob_and_indexes_it(store: DocumentStore, session: Session, tmp_path: Path) -> None:
    blob = store.put(_URL, doc_type=DocType.PDF, body=b"PDF", content_type="application/pdf")
    assert blob.body == b"PDF"
    assert blob.size_bytes == 3
    entries = session.query(DocumentCacheEntry).all()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.url == _URL
    assert entry.doc_type == "pdf"
    assert entry.content_type == "application/pdf"
    # The blob is on disk, sharded by the SHA prefix, with the doc-type extension.
    path = tmp_path / entry.cache_path
    assert path.exists() and path.read_bytes() == b"PDF"
    assert entry.cache_path.startswith(f"{entry.sha256[:2]}/") and entry.cache_path.endswith(".pdf")


def test_get_returns_cached_blob(store: DocumentStore) -> None:
    store.put(_URL, doc_type=DocType.PDF, body=b"PDF", content_type="application/pdf")
    blob = store.get(_URL)
    assert blob is not None
    assert blob.body == b"PDF"
    assert blob.doc_type is DocType.PDF
    assert blob.content_type == "application/pdf"


def test_get_miss_returns_none(store: DocumentStore) -> None:
    assert store.get("https://host/missing.pdf") is None


def test_stale_index_row_is_a_miss(store: DocumentStore, tmp_path: Path, session: Session) -> None:
    store.put(_URL, doc_type=DocType.PDF, body=b"PDF", content_type=None)
    entry = session.query(DocumentCacheEntry).one()
    # Blob evicted out-of-band, index row left behind → treated as a miss.
    (tmp_path / entry.cache_path).unlink()
    assert store.get(_URL) is None


def test_put_same_url_upserts_single_row(store: DocumentStore, session: Session) -> None:
    store.put(_URL, doc_type=DocType.PDF, body=b"v1", content_type=None)
    store.put(_URL, doc_type=DocType.PDF, body=b"v2-longer", content_type="application/pdf")
    entries = session.query(DocumentCacheEntry).all()
    assert len(entries) == 1  # deduped by URL
    assert entries[0].size_bytes == len(b"v2-longer")
    assert store.get(_URL).body == b"v2-longer"


def test_distinct_urls_get_distinct_rows(store: DocumentStore, session: Session) -> None:
    store.put("https://host/a.pdf", doc_type=DocType.PDF, body=b"a", content_type=None)
    store.put("https://host/b.html", doc_type=DocType.HTML, body=b"b", content_type=None)
    assert session.query(DocumentCacheEntry).count() == 2


# -- purge ---------------------------------------------------------------------


def test_purge_removes_blob_and_row(store: DocumentStore, session: Session, tmp_path: Path) -> None:
    store.put(_URL, doc_type=DocType.PDF, body=b"PDF", content_type=None)
    entry_path = tmp_path / session.query(DocumentCacheEntry).one().cache_path

    assert store.purge([_URL]) == 1

    assert not entry_path.exists()
    assert not entry_path.parent.exists()  # the emptied shard directory is pruned too
    assert session.query(DocumentCacheEntry).count() == 0
    assert store.get(_URL) is None


def test_purge_only_touches_the_given_urls(store: DocumentStore, session: Session) -> None:
    store.put("https://host/a.pdf", doc_type=DocType.PDF, body=b"a", content_type=None)
    store.put("https://host/b.pdf", doc_type=DocType.PDF, body=b"b", content_type=None)

    assert store.purge(["https://host/a.pdf"]) == 1

    # b/ survives: another execution's cache entries are none of this purge's business.
    assert session.query(DocumentCacheEntry).one().url == "https://host/b.pdf"
    assert store.get("https://host/b.pdf").body == b"b"


def test_purge_ignores_unknown_and_already_evicted_urls(
    store: DocumentStore, session: Session, tmp_path: Path
) -> None:
    store.put(_URL, doc_type=DocType.PDF, body=b"PDF", content_type=None)
    (tmp_path / session.query(DocumentCacheEntry).one().cache_path).unlink()  # blob gone

    # The stale row still counts as removed; the unknown URL is skipped silently.
    assert store.purge([_URL, "https://host/never-fetched.pdf"]) == 1
    assert session.query(DocumentCacheEntry).count() == 0


def test_purge_of_nothing_is_a_no_op(store: DocumentStore) -> None:
    assert store.purge([]) == 0


def test_purge_keeps_a_shared_shard_directory(store: DocumentStore, tmp_path: Path, session: Session) -> None:
    # Two URLs whose SHA-256 shares a leading byte land in the same shard; purging one must
    # not remove the directory the other still lives in.
    purged_url, kept_url = _same_shard_urls()
    for url in (purged_url, kept_url):
        store.put(url, doc_type=DocType.PDF, body=b"x", content_type=None)
    kept = session.query(DocumentCacheEntry).filter_by(url=kept_url).one()
    survivor = tmp_path / kept.cache_path

    store.purge([purged_url])

    assert survivor.exists()
    assert session.query(DocumentCacheEntry).one().url == kept_url


def _same_shard_urls() -> tuple[str, str]:
    """Two URLs whose SHA-256 digests start with the same byte (same cache shard)."""
    seen: dict[str, str] = {}
    for i in range(10_000):
        url = f"https://host/{i}.pdf"
        prefix = hashlib.sha256(url.encode("utf-8")).hexdigest()[:2]
        if prefix in seen:
            return seen[prefix], url
        seen[prefix] = url
    raise AssertionError("no colliding shard prefix found")
