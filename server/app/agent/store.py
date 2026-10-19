"""Storage for the agent: runs, proposals to store, and the profile store.

The profile store is deliberately separate from the knowledge base. "I am on
the Pro plan" is true of one user and changes without notice; putting it in
the shared, embedded knowledge base would hand it to everyone who asks a
similar question. It is small, typed and read directly, never by similarity.

There is no authentication yet, so the profile store has one owner: whoever
runs this server. It gains a user column when sign-in exists.
"""

from __future__ import annotations

import json
import uuid

from app.agent.claims import Run
from app.db.database import get_connection

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_runs (
    id         TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    status     TEXT NOT NULL,
    state      TEXT NOT NULL,             -- the whole Run as JSON
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_session
    ON agent_runs(session_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS agent_proposals (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    destination TEXT NOT NULL CHECK (destination IN ('profile', 'knowledge')),
    key         TEXT,                     -- profile only
    value       TEXT,                     -- profile only
    title       TEXT,                     -- knowledge only
    note        TEXT,                     -- knowledge only
    warning     TEXT,                     -- set when the documents disagree
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | saved | discarded
    document_id TEXT,                     -- set once a knowledge note is indexed
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_agent_proposals_session
    ON agent_proposals(session_id, status);

CREATE TABLE IF NOT EXISTS profile_facts (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    source     TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def init_agent_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        # Added after the first release; older tables gain it here.
        columns = {r[1] for r in conn.execute("PRAGMA table_info(agent_proposals)")}
        if "warning" not in columns:
            conn.execute("ALTER TABLE agent_proposals ADD COLUMN warning TEXT")


# ----------------------------------------------------------------------- runs


def save_run(run: Run) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO agent_runs (id, session_id, status, state)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                state = excluded.state,
                updated_at = datetime('now')
            """,
            (run.id, run.session_id, run.status, json.dumps(run.to_dict())),
        )


def load_run(run_id: str) -> Run | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT state FROM agent_runs WHERE id = ?", (run_id,)
        ).fetchone()
    return Run.from_dict(json.loads(row["state"])) if row else None


def awaiting_run(session_id: str) -> Run | None:
    """The run in this session that is waiting on the user's answers, if any."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT state FROM agent_runs WHERE session_id = ? AND status = 'awaiting' "
            "ORDER BY updated_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    return Run.from_dict(json.loads(row["state"])) if row else None


def session_runs(session_id: str, limit: int = 40) -> list[Run]:
    """A session's most recent runs, for redrawing their reasoning."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT state FROM agent_runs WHERE session_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    return [Run.from_dict(json.loads(r["state"])) for r in rows]


def abandon_awaiting(session_id: str) -> None:
    """A new question supersedes any run still waiting for answers."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE agent_runs SET status = 'abandoned', updated_at = datetime('now') "
            "WHERE session_id = ? AND status = 'awaiting'",
            (session_id,),
        )


# ------------------------------------------------------------------ proposals


def add_proposals(run_id: str, session_id: str, items: list[dict]) -> list[dict]:
    saved = []
    with get_connection() as conn:
        for item in items:
            proposal_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO agent_proposals "
                "(id, run_id, session_id, destination, key, value, title, note, warning) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    proposal_id,
                    run_id,
                    session_id,
                    item["destination"],
                    item.get("key"),
                    item.get("value"),
                    item.get("title"),
                    item.get("note"),
                    item.get("warning"),
                ),
            )
            saved.append({"id": proposal_id, "status": "pending", **item})
    return saved


def get_proposal(proposal_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM agent_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
    return dict(row) if row else None


def pending_proposals(session_id: str) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_proposals WHERE session_id = ? AND status = 'pending' "
            "ORDER BY created_at",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def resolve_proposal(
    proposal_id: str,
    status: str,
    *,
    document_id: str | None = None,
    **fields: str | None,
) -> dict | None:
    """Mark a proposal saved or discarded, keeping any edits made before saving."""
    allowed = {k: v for k, v in fields.items() if k in ("key", "value", "title", "note")}
    sets = ["status = ?", "document_id = COALESCE(?, document_id)"]
    values: list = [status, document_id]
    for column, value in allowed.items():
        if value is not None:
            sets.append(f"{column} = ?")
            values.append(value)
    values.append(proposal_id)
    with get_connection() as conn:
        conn.execute(
            f"UPDATE agent_proposals SET {', '.join(sets)} WHERE id = ?", values
        )
    return get_proposal(proposal_id)


# -------------------------------------------------------------------- profile


def profile_all() -> dict[str, str]:
    with get_connection() as conn:
        rows = conn.execute("SELECT key, value FROM profile_facts ORDER BY key").fetchall()
    return {r["key"]: r["value"] for r in rows}


def profile_list() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT key, value, source, updated_at FROM profile_facts ORDER BY key"
        ).fetchall()
    return [dict(r) for r in rows]


def profile_set(key: str, value: str, source: str = "user") -> None:
    """Newest wins: a profile fact is overwritten, never versioned."""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO profile_facts (key, value, source) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                source = excluded.source,
                updated_at = datetime('now')
            """,
            (key, value, source),
        )


def profile_delete(key: str) -> bool:
    with get_connection() as conn:
        return conn.execute("DELETE FROM profile_facts WHERE key = ?", (key,)).rowcount > 0
