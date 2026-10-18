"""SQLite connection handling and schema.

A fresh connection per operation, rather than one shared connection: SQLite
connections are not safe to use across threads, and FastAPI serves synchronous
routes from a worker threadpool. Connections are cheap; correctness is not.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from app.config import settings

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT 'New chat',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role       TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant')),
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Every read of a transcript filters by session_id and orders by id.
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

-- The sidebar lists sessions by most recent activity.
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);
"""


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """Yield a connection, committing on success and rolling back on error."""
    conn = sqlite3.connect(settings.db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row

    # Must be set on *every* connection: SQLite defaults foreign key
    # enforcement to OFF per-connection, so without this the
    # ON DELETE CASCADE above silently does nothing and deleting a session
    # leaves its messages behind as orphans.
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create the schema if it does not exist. Safe to call on every startup."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(settings.db_path, timeout=10.0)
    try:
        # WAL lets a reader proceed while a writer holds the database, which
        # matters here because a long streaming reply writes at the end while
        # the sidebar is being read. It is persistent, so setting it once is enough.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()

    logger.info("Database ready at %s", settings.db_path)
