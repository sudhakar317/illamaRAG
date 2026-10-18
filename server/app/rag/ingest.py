"""Turning a file or link into indexed, searchable chunks.

Indexing runs on a background thread because it is slow here: embedding a few
hundred chunks on four CPU cores takes real time, and an upload request that
waited for it would look hung. The document row carries a status, and every
change to it is pushed to connected clients through rag/events.py.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from app.config import settings
from app.rag import embedder, events, store
from app.rag.chunker import chunk_document, contextualise
from app.rag.loaders import (
    LoadedDocument,
    LoaderError,
    load_file,
    load_url,
)

logger = logging.getLogger(__name__)

# Small batches keep peak memory flat and let progress be logged as it goes.
EMBED_BATCH = 16

# Indexing holds the embedding model; running several at once would just
# contend for the same lock and the same four cores.
_index_lock = threading.Lock()


def _announce(document_id: str) -> None:
    """Push the document's current row to every listening client."""
    document = store.get_document(document_id)
    if document is not None:
        events.publish({"type": "document", "document": document})


def _set_status(document_id: str, status: str, **fields) -> None:  # type: ignore[no-untyped-def]
    store.set_status(document_id, status, **fields)
    _announce(document_id)


def _index(document_id: str, document: LoadedDocument) -> None:
    """Chunk, embed and store. Runs on a worker thread."""
    with _index_lock:
        try:
            _set_status(document_id, "indexing")

            chunks = chunk_document(
                document,
                chunk_chars=settings.chunk_chars,
                overlap_chars=settings.chunk_overlap_chars,
            )
            if not chunks:
                _set_status(
                    document_id, "failed", error="No text could be extracted."
                )
                return

            logger.info(
                "Indexing %r: %d chunks, %d chars",
                document.title,
                len(chunks),
                document.total_chars(),
            )

            total = 0
            for start in range(0, len(chunks), EMBED_BATCH):
                batch = chunks[start : start + EMBED_BATCH]

                # Each chunk is embedded with its document and section name
                # attached, but stored without them, so retrieval benefits from
                # the context while the prompt shows only the passage itself.
                vectors = embedder.embed_documents(
                    [contextualise(c, document.title) for c in batch]
                )
                store.add_chunks(
                    document_id,
                    [
                        {
                            "text": c.text,
                            "idx": c.index,
                            "page": c.page,
                            "section": c.section,
                        }
                        for c in batch
                    ],
                    vectors,
                )
                total += len(batch)
                _set_status(document_id, "indexing", chunk_count=total)
                logger.info("  %d/%d chunks embedded", total, len(chunks))

            _set_status(
                document_id,
                "indexed",
                chunk_count=len(chunks),
                char_count=document.total_chars(),
            )
            logger.info("Indexed %r (%d chunks).", document.title, len(chunks))

        except Exception as exc:  # noqa: BLE001 - must land on the document row
            logger.exception("Indexing failed for document %s", document_id)
            _set_status(document_id, "failed", error=str(exc)[:500])


def _start(document_id: str, document: LoadedDocument) -> None:
    threading.Thread(
        target=_index,
        args=(document_id, document),
        name=f"index-{document_id[:8]}",
        daemon=True,
    ).start()


def ingest_file(path: Path, original_name: str) -> dict:
    """Load a saved upload and begin indexing it."""
    try:
        document = load_file(path, display_name=original_name)
    except LoaderError:
        path.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001
        path.unlink(missing_ok=True)
        raise LoaderError(f"Could not read that file: {exc}") from exc

    document_id = store.create_document(
        title=document.title or original_name,
        source_type=document.source_type,
        source=original_name,
    )
    _announce(document_id)
    _start(document_id, document)
    return store.get_document(document_id)  # type: ignore[return-value]


def ingest_url(url: str) -> dict:
    """Fetch a page and begin indexing its main content."""
    document = load_url(url, timeout=settings.url_fetch_timeout)
    document_id = store.create_document(
        title=document.title or url,
        source_type="url",
        source=document.url or url,
    )
    _announce(document_id)
    _start(document_id, document)
    return store.get_document(document_id)  # type: ignore[return-value]
