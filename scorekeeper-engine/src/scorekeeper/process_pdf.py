"""Procesa un único PDF con el pipeline de recuperación (docs/retrieval-pipeline.md).

Salta las etapas de red (locate/authorize/fetch) — el documento ya está en disco — y
ejecuta extract (unstructured-api: markdown con tablas + oraciones), chunk (ventanas
solapadas) y, opcionalmente, embed.

Como librería::

    from scorekeeper.process_pdf import process_pdf

    result = process_pdf(Path("documento.pdf"), page=3, embed=True)

Como script::

    python -m scorekeeper.process_pdf documento.pdf --page 3 --embed --output salida.json

Los logs salen por stdout con el formato del resto de la app; ``--debug`` los baja a
DEBUG (peticiones a ``unstructured-api`` incluidas) y sin él manda ``LOG_LEVEL``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel

from scorekeeper.config.settings import get_settings
from scorekeeper.core.retrieval.chunk import SentenceWindow, chunk_sentences
from scorekeeper.core.retrieval.embed import Embedder, EmbedError, OpenAIEmbedder
from scorekeeper.core.retrieval.extract import ExtractError
from scorekeeper.core.retrieval.extract_unstructured import UnstructuredContentExtractor
from scorekeeper.core.retrieval.types import (
    DocType,
    DocumentLocator,
    ExtractedContent,
    FetchedDocument,
    RetrievedDocument,
    SourceRef,
)
from scorekeeper.utils.logging_config import configure_logging


class ProcessedPDF(BaseModel):
    """Resultado del pipeline sobre un PDF local.

    ``embeddings`` tiene un elemento por chunk, ``None`` cuando no se pidió embedding.
    """

    document: RetrievedDocument
    extracted: ExtractedContent
    chunks: list[SentenceWindow]
    embeddings: list[list[float] | None]

    def to_payload(self) -> dict:
        """Serializa documento y chunks (con su embedding) a JSON-compatible."""
        return {
            "document": self.document.model_dump(),
            "chunks": [
                {**window.model_dump(), "chunk_index": index, "embedding": vector}
                for index, (window, vector) in enumerate(
                    zip(self.chunks, self.embeddings, strict=True)
                )
            ],
        }


@contextmanager
def _recursion_limit(limit: int | None):
    """Eleva el límite de recursión durante el bloque y lo restaura al salir.

    Es estado **global del proceso**: afecta a todo lo que corra en paralelo mientras dure
    el bloque, y un límite muy alto puede agotar la pila del hilo (segfault) en vez de
    lanzar ``RecursionError``. Por eso solo se aplica cuando se pide explícitamente.
    """
    if limit is None:
        yield
        return
    previous = sys.getrecursionlimit()
    sys.setrecursionlimit(limit)
    try:
        yield
    finally:
        sys.setrecursionlimit(previous)


def process_pdf(
    pdf: Path,
    *,
    page: int | None = None,
    chunk_size: int | None = None,
    overlap: int | None = None,
    embed: bool = False,
    embedder: Embedder | None = None,
    recursion_limit: int | None = None,
) -> ProcessedPDF:
    """Extrae, trocea y (opcionalmente) embebe ``pdf``.

    ``chunk_size``/``overlap`` toman los valores de ``Settings`` cuando son ``None``.
    ``recursion_limit`` eleva el límite de recursión de Python solo durante la extracción:
    ``pypdf`` recursa al parsear arrays muy anidados y algunos PDFs fallan con
    ``RecursionError`` (envuelto en ``ExtractError``) con el límite por defecto. Ver
    :func:`_recursion_limit` para sus riesgos.

    La extracción va contra ``unstructured-api``: requiere ``UNSTRUCTURED_API_URL``.

    Propaga ``ExtractError`` si falla la extracción, ``ValueError`` si los parámetros de
    chunking son inválidos y ``EmbedError`` si falla el cálculo de embeddings.
    """
    settings = get_settings()
    size = settings.embedding_chunk_sentences if chunk_size is None else chunk_size
    step_overlap = settings.embedding_chunk_overlap if overlap is None else overlap

    source = SourceRef(name=pdf.name, url=pdf.resolve().as_uri(), rank=0)
    locator = DocumentLocator(
        document_url=source.url,
        filename=pdf.name,
        doc_type=DocType.PDF,
        scheme="file",
        host="",
        page=page,
    )
    document = FetchedDocument(
        document_url=locator.document_url, doc_type=DocType.PDF, body=pdf.read_bytes()
    )

    with _recursion_limit(recursion_limit):
        extracted = UnstructuredContentExtractor().extract(document, locator)
    windows = chunk_sentences(extracted.sentences, size=size, overlap=step_overlap)

    vectors: list[list[float] | None] = [None] * len(windows)
    if embed and windows:
        vectors = list((embedder or OpenAIEmbedder()).embed([w.text for w in windows]))

    return ProcessedPDF(
        document=extracted.to_document(source, locator),
        extracted=extracted,
        chunks=windows,
        embeddings=vectors,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pdf", type=Path, help="Ruta del archivo .pdf a procesar")
    parser.add_argument(
        "--page", type=int, default=None,
        help="Página a extraer (1-based, como #page=N); por defecto se extraen todas",
    )
    parser.add_argument(
        "--chunk-size", type=int, default=settings.embedding_chunk_sentences,
        help="Oraciones por chunk (por defecto %(default)s)",
    )
    parser.add_argument(
        "--overlap", type=int, default=settings.embedding_chunk_overlap,
        help="Oraciones compartidas con el chunk anterior (por defecto %(default)s)",
    )
    parser.add_argument(
        "--recursion-limit", type=int, default=None,
        help="Límite de recursión de Python durante la extracción (para PDFs muy anidados)",
    )
    parser.add_argument(
        "--embed", action="store_true",
        help="Calcula el embedding de cada chunk (requiere OPENAI_API_KEY)",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Logs a nivel DEBUG, incluidas las peticiones a unstructured-api",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Archivo JSON de salida; sin él se imprime un resumen en stdout",
    )
    return parser.parse_args(argv)


def _configure_logging(debug: bool) -> None:
    """Deja los logs del pipeline en stdout; con ``debug`` baja todo a DEBUG.

    Sin esto el script corre con la configuración por defecto de ``logging`` y solo se ven
    los WARNING sueltos, sin formato. ``configure_logging`` silencia ``httpx`` a WARNING
    para que los juicios no inunden la salida; aquí ese ruido es justo lo que se quiere
    ver, así que en modo debug se revierte.
    """
    if not debug:
        configure_logging(get_settings().log_level)
        return
    configure_logging(logging.DEBUG, force=True)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.DEBUG)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _configure_logging(args.debug)
    if not args.pdf.is_file():
        print(f"No existe el archivo: {args.pdf}", file=sys.stderr)
        return 1

    try:
        result = process_pdf(
            args.pdf,
            page=args.page,
            chunk_size=args.chunk_size,
            overlap=args.overlap,
            embed=args.embed,
            recursion_limit=args.recursion_limit,
        )
    except ExtractError as exc:
        print(f"Falló la extracción: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Parámetros de chunking inválidos: {exc}", file=sys.stderr)
        return 1
    except EmbedError as exc:
        print(f"No se pudieron calcular los embeddings: {exc}", file=sys.stderr)
        return 1

    if args.output is None:
        print(result.extracted.text)
        print(
            f"\n--- {len(result.extracted.sentences)} oración(es), {len(result.chunks)} chunk(s)"
            f"{' con embedding' if args.embed else ''} ---",
            file=sys.stderr,
        )
        return 0

    args.output.write_text(
        json.dumps(result.to_payload(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"{len(result.chunks)} chunk(s) escritos en {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
