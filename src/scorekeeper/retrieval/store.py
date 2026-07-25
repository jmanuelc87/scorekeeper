"""Local filesystem blob store for fetched documents, indexed by ``document_cache``.

The fetch stage downloads a document at most once *within* a platform execution: bytes are
written under a cache root and recorded in the ``document_cache`` table (one row per source
``url``), so later references to the same URL read the blob off disk instead of the network.
The cache is **not** retained across platform executions — :meth:`DocumentStore.purge` drops
the blobs (and their index rows) once retrieval for an execution is done.

Blobs are sharded into subdirectories by the leading hex of the URL's SHA-256 to keep any one
directory small; the DB row stores the path relative to the cache root, so the root can move.
The store follows the codebase's session-injection convention (``session`` defaults to a fresh
``SessionLocal()`` per operation; tests inject an in-memory session).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.db.connection import session_scope
from scorekeeper.db.models import DocumentCacheEntry
from scorekeeper.retrieval.types import DocType

# Blob filename extension per document type (cosmetic; the SHA is the real key).
_EXTENSIONS = {DocType.PDF: ".pdf", DocType.HTML: ".html", DocType.UNKNOWN: ".bin"}


@dataclass
class CachedBlob:
    """A document's bytes plus its cache metadata."""

    body: bytes
    doc_type: DocType
    content_type: str | None
    sha256: str
    size_bytes: int


class DocumentStore:
    """Filesystem blob cache indexed by the ``document_cache`` table."""

    def __init__(self, root: str | Path, *, session: AsyncSession | None = None) -> None:
        self._root = Path(root)
        self._session = session

    @staticmethod
    def _sha(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def _relpath(self, sha: str, doc_type: DocType) -> str:
        return f"{sha[:2]}/{sha}{_EXTENSIONS.get(doc_type, '.bin')}"

    async def get(self, url: str) -> CachedBlob | None:
        """Return the cached blob for ``url``, or ``None`` on a miss.

        A ``document_cache`` row whose blob file is missing (evicted out-of-band) is treated
        as a miss so the caller re-downloads.
        """
        async with session_scope(self._session) as db:
            entry = await db.scalar(
                select(DocumentCacheEntry).where(DocumentCacheEntry.url == url)
            )
            if entry is None:
                return None
            path = self._root / entry.cache_path
            if not path.exists():
                return None
            return CachedBlob(
                body=path.read_bytes(),
                doc_type=DocType(entry.doc_type),
                content_type=entry.content_type,
                sha256=entry.sha256,
                size_bytes=entry.size_bytes,
            )

    async def put(
        self,
        url: str,
        *,
        doc_type: DocType,
        body: bytes,
        content_type: str | None,
    ) -> CachedBlob:
        """Write ``body`` to the cache and upsert its ``document_cache`` row."""
        sha = self._sha(url)
        relpath = self._relpath(sha, doc_type)
        path = self._root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

        async with session_scope(self._session) as db:
            entry = await db.scalar(
                select(DocumentCacheEntry).where(DocumentCacheEntry.url == url)
            )
            if entry is None:
                entry = DocumentCacheEntry(url=url)
                db.add(entry)
            entry.sha256 = sha
            entry.cache_path = relpath
            entry.doc_type = doc_type.value
            entry.content_type = content_type
            entry.size_bytes = len(body)
            await db.commit()

        return CachedBlob(
            body=body,
            doc_type=doc_type,
            content_type=content_type,
            sha256=sha,
            size_bytes=len(body),
        )

    async def purge(self, urls: Iterable[str]) -> int:
        """Delete the cached blobs for ``urls`` and their index rows; return how many went.

        Scoped on purpose: only the URLs handed in are removed, so a concurrent execution's
        entries survive. Unknown URLs and rows whose blob is already gone are skipped without
        error (the goal is that nothing remains, not that everything was there). Empty shard
        directories are pruned so the cache root does not fill with husks.
        """
        removed = 0
        async with session_scope(self._session) as db:
            for url in set(urls):
                entry = await db.scalar(
                    select(DocumentCacheEntry).where(DocumentCacheEntry.url == url)
                )
                if entry is None:
                    continue
                self._unlink(self._root / entry.cache_path)
                await db.delete(entry)
                removed += 1
            await db.commit()
        return removed

    @staticmethod
    def _unlink(path: Path) -> None:
        """Remove a blob and its shard directory when that leaves the directory empty."""
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:  # not empty (other blobs share the shard), or already gone
            pass
