"""Request and response models for the HTTP API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SessionCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class SessionRename(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class SessionOut(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    created_at: str
    sources: list[dict] | None = None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    # Optional: when absent or unknown, the server creates a session and
    # announces its id in the first SSE event.
    session_id: str | None = None

    # Search the knowledge base before answering. Retrieval is skipped
    # automatically when nothing has been indexed yet.
    use_rag: bool = True
