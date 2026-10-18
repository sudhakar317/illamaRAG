"""Storage for the knowledge base: chunk text, keyword index and vectors.

Two indexes are maintained over the same chunks, because they fail in
different ways and cover for each other:

- **Vectors** (Chroma, cosine over nomic embeddings) match on meaning, so
  "how do I get my money back" finds a passage about refunds. They are weak on
  rare literal strings — product codes, surnames, version numbers — because
  those carry little semantic signal.
- **Keywords** (SQLite FTS5, BM25) match literal terms exactly, which is
  precisely where vectors struggle, but miss any paraphrase.

SQLite holds the authoritative chunk text; Chroma stores only vectors and ids,
so the two can never disagree about what a chunk says.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.db.database import get_connection

logger = logging.getLogger(__name__)

COLLECTION_NAME = "knowledge"

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    source_type  TEXT NOT NULL,          -- pdf | text | html | url
    source       TEXT,                   -- original filename or URL
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending|indexing|indexed|failed
    error        TEXT,
    chunk_count  INTEGER NOT NULL DEFAULT 0,
    char_count   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,
    text        TEXT NOT NULL,
    page        INTEGER,
    section     TEXT
);

CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id, idx);

-- External-content FTS index: the text lives in `chunks`, and this holds only
-- the inverted index, so chunk text is never stored twice.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    content='chunks',
    content_rowid='id',
    tokenize='porter unicode61'
);
"""


@dataclass
class ChunkRow:
    id: int
    document_id: int | str
    idx: int
    text: str
    page: int | None
    section: str | None
    title: str
    source_type: str
    source: str | None


def init_rag_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)
    logger.info("Knowledge base tables ready.")


# ----------------------------------------------------------------- vectors

_collection: Any = None


def get_collection():
    """The Chroma collection, created on first use."""
    global _collection
    if _collection is None:
        import chromadb
        from chromadb.config import Settings as ChromaSettings

        settings.vector_dir.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(
            path=str(settings.vector_dir),
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        _collection = client.get_or_create_collection(
            name=COLLECTION_NAME,
            # We supply our own vectors; without this Chroma would quietly
            # download and use its own default embedding model.
            embedding_function=None,
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


# --------------------------------------------------------------- documents


def create_document(title: str, source_type: str, source: str | None) -> str:
    document_id = str(uuid.uuid4())
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO documents (id, title, source_type, source, status) "
            "VALUES (?, ?, ?, ?, 'pending')",
            (document_id, title, source_type, source),
        )
    return document_id


def set_status(
    document_id: str,
    status: str,
    *,
    error: str | None = None,
    chunk_count: int | None = None,
    char_count: int | None = None,
) -> None:
    fields = ["status = ?"]
    values: list[Any] = [status]
    if error is not None:
        fields.append("error = ?")
        values.append(error)
    if chunk_count is not None:
        fields.append("chunk_count = ?")
        values.append(chunk_count)
    if char_count is not None:
        fields.append("char_count = ?")
        values.append(char_count)
    values.append(document_id)

    with get_connection() as conn:
        conn.execute(f"UPDATE documents SET {', '.join(fields)} WHERE id = ?", values)


def get_document(document_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
    return dict(row) if row else None


def list_documents() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM documents ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def count_documents() -> tuple[int, int]:
    """(documents, indexed chunks)"""
    with get_connection() as conn:
        docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    return int(docs), int(chunks)


def delete_document(document_id: str) -> bool:
    """Remove a document, its chunks, its FTS rows and its vectors."""
    with get_connection() as conn:
        chunk_ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM chunks WHERE document_id = ?", (document_id,)
            ).fetchall()
        ]
        # The FTS index is external-content, so it does not follow the
        # foreign-key cascade and has to be cleared explicitly first --
        # otherwise deleted text stays searchable.
        for chunk_id in chunk_ids:
            conn.execute(
                "INSERT INTO chunks_fts(chunks_fts, rowid, text) "
                "VALUES ('delete', ?, (SELECT text FROM chunks WHERE id = ?))",
                (chunk_id, chunk_id),
            )
        cursor = conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
        deleted = cursor.rowcount > 0

    if chunk_ids:
        try:
            get_collection().delete(ids=[str(c) for c in chunk_ids])
        except Exception:  # noqa: BLE001 - SQLite is the source of truth
            logger.exception("Could not delete vectors for document %s", document_id)

    return deleted


# ------------------------------------------------------------------ chunks


def add_chunks(
    document_id: str,
    chunks: list[dict],
    embeddings: list[list[float]],
) -> list[int]:
    """Insert chunks into SQLite, the FTS index and Chroma.

    `chunks` items: {text, idx, page, section, embed_text}
    """
    if not chunks:
        return []

    ids: list[int] = []
    with get_connection() as conn:
        for chunk in chunks:
            cursor = conn.execute(
                "INSERT INTO chunks (document_id, idx, text, page, section) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    document_id,
                    chunk["idx"],
                    chunk["text"],
                    chunk.get("page"),
                    chunk.get("section"),
                ),
            )
            chunk_id = int(cursor.lastrowid)
            ids.append(chunk_id)
            conn.execute(
                "INSERT INTO chunks_fts(rowid, text) VALUES (?, ?)",
                (chunk_id, chunk["text"]),
            )

    get_collection().upsert(
        ids=[str(i) for i in ids],
        embeddings=embeddings,
        metadatas=[{"document_id": document_id, "idx": c["idx"]} for c in chunks],
    )
    return ids


def get_chunks(chunk_ids: list[int]) -> dict[int, ChunkRow]:
    if not chunk_ids:
        return {}
    placeholders = ",".join("?" * len(chunk_ids))
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT c.id, c.document_id, c.idx, c.text, c.page, c.section,
                   d.title, d.source_type, d.source
              FROM chunks c JOIN documents d ON d.id = c.document_id
             WHERE c.id IN ({placeholders})
            """,
            chunk_ids,
        ).fetchall()
    return {r["id"]: ChunkRow(**dict(r)) for r in rows}


def get_neighbours(document_id: str, idx: int, window: int) -> list[ChunkRow]:
    """Chunks immediately before and after one that matched."""
    if window <= 0:
        return []
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.document_id, c.idx, c.text, c.page, c.section,
                   d.title, d.source_type, d.source
              FROM chunks c JOIN documents d ON d.id = c.document_id
             WHERE c.document_id = ? AND c.idx BETWEEN ? AND ? AND c.idx != ?
          ORDER BY c.idx
            """,
            (document_id, idx - window, idx + window, idx),
        ).fetchall()
    return [ChunkRow(**dict(r)) for r in rows]


# ------------------------------------------------------------------ search


def dense_search(query_vector: list[float], limit: int) -> list[tuple[int, float]]:
    """Vector search. Returns (chunk_id, cosine similarity), best first."""
    try:
        result = get_collection().query(
            query_embeddings=[query_vector],
            n_results=limit,
            include=["distances"],
        )
    except Exception:  # noqa: BLE001
        logger.exception("Vector search failed")
        return []

    ids = (result.get("ids") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]
    # Chroma reports cosine *distance*; similarity is 1 - distance.
    return [(int(i), 1.0 - float(d)) for i, d in zip(ids, distances)]


_WORD = re.compile(r"[0-9A-Za-z_À-ɏ]+")
# Words carrying no retrieval signal; dropping them stops BM25 ranking on
# "what" and "the" when the real query terms are rarer.
_STOPWORDS = frozenset(
    """a an and are as at be by do does for from how i in is it of on or
    that the this to was what when where which who why will with you your""".split()
)


def _fts_query(text: str) -> str:
    """Build a safe FTS5 MATCH expression from free-form user text.

    User text goes nowhere near the FTS grammar directly: characters like
    `"`, `*`, `-` and `NEAR` are operators there, so an ordinary question would
    otherwise raise a syntax error. Every term is quoted as a literal and
    OR-ed, letting BM25 rank by how many rare terms a chunk contains.
    """
    words = [w.lower() for w in _WORD.findall(text)]
    terms = [w for w in words if len(w) > 1 and w not in _STOPWORDS]
    if not terms:
        terms = [w for w in words if len(w) > 1]
    if not terms:
        return ""
    unique = list(dict.fromkeys(terms))[:24]
    return " OR ".join(f'"{t}"' for t in unique)


def keyword_search(query: str, limit: int) -> list[tuple[int, float]]:
    """BM25 search. Returns (chunk_id, score) with higher being better."""
    match = _fts_query(query)
    if not match:
        return []
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT rowid AS id, bm25(chunks_fts) AS rank
                  FROM chunks_fts
                 WHERE chunks_fts MATCH ?
              ORDER BY rank
                 LIMIT ?
                """,
                (match, limit),
            ).fetchall()
    except sqlite3.OperationalError:
        logger.exception("Keyword search failed for %r", match)
        return []

    # SQLite's bm25() returns a negative number, more negative being a better
    # match. Flip the sign so larger is better, consistent with dense_search.
    return [(int(r["id"]), -float(r["rank"])) for r in rows]
