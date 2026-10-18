"""All reads and writes for sessions and messages.

Routes never touch SQL directly, so the storage shape stays changeable in one
place.
"""

from __future__ import annotations

import json
import uuid

from app.db.database import get_connection

DEFAULT_TITLE = "New chat"
TITLE_MAX_CHARS = 50


# ---------------------------------------------------------------- sessions


def create_session(title: str | None = None, system_prompt: str | None = None) -> dict:
    """Create a session and optionally seed its system message."""
    session_id = str(uuid.uuid4())
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO sessions (id, title) VALUES (?, ?)",
            (session_id, title or DEFAULT_TITLE),
        )
        if system_prompt:
            conn.execute(
                "INSERT INTO messages (session_id, role, content) VALUES (?, 'system', ?)",
                (session_id, system_prompt),
            )
    return get_session(session_id)  # type: ignore[return-value]


def session_exists(session_id: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    return row is not None


def get_session(session_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT s.id, s.title, s.created_at, s.updated_at,
                   (SELECT COUNT(*) FROM messages m
                     WHERE m.session_id = s.id AND m.role != 'system') AS message_count
              FROM sessions s
             WHERE s.id = ?
            """,
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def list_sessions() -> list[dict]:
    """Sessions ordered by most recent activity, for the sidebar."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.title, s.created_at, s.updated_at,
                   (SELECT COUNT(*) FROM messages m
                     WHERE m.session_id = s.id AND m.role != 'system') AS message_count
              FROM sessions s
          ORDER BY s.updated_at DESC, s.created_at DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def rename_session(session_id: str, title: str) -> dict | None:
    title = (title or "").strip() or DEFAULT_TITLE
    with get_connection() as conn:
        conn.execute(
            "UPDATE sessions SET title = ?, updated_at = datetime('now') WHERE id = ?",
            (title, session_id),
        )
    return get_session(session_id)


def delete_session(session_id: str) -> bool:
    """Delete a session; its messages go with it via ON DELETE CASCADE."""
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return cursor.rowcount > 0


def clear_session(session_id: str) -> None:
    """Drop the conversation but keep the session and its system message."""
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM messages WHERE session_id = ? AND role != 'system'",
            (session_id,),
        )
        conn.execute(
            "UPDATE sessions SET updated_at = datetime('now') WHERE id = ?",
            (session_id,),
        )


# ---------------------------------------------------------------- messages


def add_message(
    session_id: str,
    role: str,
    content: str,
    sources: list[dict] | None = None,
) -> int:
    """Append a message and mark the session as recently active.

    Bumping updated_at here is what keeps the sidebar ordered by real activity;
    SQLite has no automatic "on update" timestamp.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO messages (session_id, role, content, sources) "
            "VALUES (?, ?, ?, ?)",
            (
                session_id,
                role,
                content,
                json.dumps(sources) if sources else None,
            ),
        )
        conn.execute(
            "UPDATE sessions SET updated_at = datetime('now') WHERE id = ?",
            (session_id,),
        )
        return int(cursor.lastrowid)


def get_messages(session_id: str, include_system: bool = False) -> list[dict]:
    """The full transcript, oldest first, with citations decoded."""
    sql = (
        "SELECT id, role, content, sources, created_at "
        "FROM messages WHERE session_id = ?"
    )
    if not include_system:
        sql += " AND role != 'system'"
    sql += " ORDER BY id ASC"

    with get_connection() as conn:
        rows = conn.execute(sql, (session_id,)).fetchall()

    messages = []
    for row in rows:
        message = dict(row)
        raw = message.pop("sources", None)
        try:
            message["sources"] = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            message["sources"] = None
        messages.append(message)
    return messages


def get_history_for_model(session_id: str) -> list[dict]:
    """Prior turns as {role, content}, excluding the system message.

    The system prompt is supplied separately at generation time so that
    changing it in config applies to existing sessions too.
    """
    return [
        {"role": m["role"], "content": m["content"]}
        for m in get_messages(session_id, include_system=False)
    ]


def count_sessions() -> int:
    with get_connection() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])


# ---------------------------------------------------------------- titling


def derive_title(message: str) -> str:
    """A readable sidebar label taken from the first user message."""
    cleaned = " ".join((message or "").split())
    if not cleaned:
        return DEFAULT_TITLE
    if len(cleaned) <= TITLE_MAX_CHARS:
        return cleaned
    # Prefer cutting at a word boundary, but only if one is reasonably close.
    cut = cleaned[:TITLE_MAX_CHARS]
    space = cut.rfind(" ")
    if space > TITLE_MAX_CHARS * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,.;:!?-") + "..."


def autotitle_if_needed(session_id: str, first_message: str) -> None:
    """Title a still-untitled session from its first user message."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT title FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None or row["title"] != DEFAULT_TITLE:
            return
        conn.execute(
            "UPDATE sessions SET title = ? WHERE id = ?",
            (derive_title(first_message), session_id),
        )
