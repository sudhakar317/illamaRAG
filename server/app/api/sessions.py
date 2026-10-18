"""Session and transcript routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.config import settings
from app.db import repository as repo
from app.schemas.chat import MessageOut, SessionCreate, SessionOut, SessionRename

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.post("", response_model=SessionOut, status_code=201)
def create_session(body: SessionCreate | None = None) -> dict:
    title = body.title if body else None
    return repo.create_session(title=title, system_prompt=settings.system_prompt)


@router.get("", response_model=list[SessionOut])
def list_sessions() -> list[dict]:
    return repo.list_sessions()


@router.get("/{session_id}", response_model=SessionOut)
def get_session(session_id: str) -> dict:
    session = repo.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.get("/{session_id}/messages", response_model=list[MessageOut])
def get_messages(session_id: str) -> list[dict]:
    if not repo.session_exists(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return repo.get_messages(session_id)


@router.patch("/{session_id}", response_model=SessionOut)
def rename_session(session_id: str, body: SessionRename) -> dict:
    if not repo.session_exists(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    session = repo.rename_session(session_id, body.title)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.delete("/{session_id}", status_code=204)
def delete_session(session_id: str) -> None:
    if not repo.delete_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")


@router.post("/{session_id}/clear", response_model=SessionOut)
def clear_session(session_id: str) -> dict:
    if not repo.session_exists(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    repo.clear_session(session_id)
    return repo.get_session(session_id)  # type: ignore[return-value]
