"""Knowledge base routes: upload files, add links, list and delete."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.rag import events, ingest, retriever, store
from app.rag.loaders import SUPPORTED_EXTENSIONS, LoaderError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/documents", tags=["documents"])

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


class UrlRequest(BaseModel):
    url: str = Field(min_length=4, max_length=2048)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=4, ge=1, le=20)


def _safe_name(filename: str) -> str:
    """A filesystem-safe name that cannot escape the uploads directory.

    Only the basename is kept, so a crafted name like `../../app/main.py`
    cannot be used to write outside the uploads folder.
    """
    base = Path(filename or "upload").name
    cleaned = _UNSAFE.sub("_", base).strip("._") or "upload"
    return cleaned[:120]


def _display_name(filename: str) -> str:
    """The name to show the user, which is not the name used on disk.

    Spaces and punctuation are fine in a citation and awful in a path, so the
    two are kept separate: this keeps "Coffee Handbook.md" readable while
    _safe_name writes it as "Coffee_Handbook.md".
    """
    base = Path(filename or "upload").name
    # Strip control characters only; they have no business in a filename and
    # would render strangely wherever the title is displayed.
    cleaned = "".join(c for c in base if c.isprintable()).strip()
    return (cleaned or "upload")[:150]


@router.post("", status_code=201)
async def upload_document(file: UploadFile = File(...)) -> dict:
    display = _display_name(file.filename or "upload")
    name = _safe_name(file.filename or "upload")
    suffix = Path(name).suffix.lower()

    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported file type '{suffix or 'unknown'}'. "
                f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
            ),
        )

    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    # Prefixed with a uuid so two uploads of the same filename cannot collide.
    target = settings.uploads_dir / f"{uuid.uuid4().hex[:8]}_{name}"

    try:
        with target.open("wb") as out:
            shutil.copyfileobj(file.file, out, length=1024 * 1024)
    finally:
        await file.close()

    size_mb = target.stat().st_size / (1024**2)
    if size_mb > settings.max_upload_mb:
        target.unlink(missing_ok=True)
        raise HTTPException(
            status_code=413,
            detail=f"File is {size_mb:.1f} MB; the limit is {settings.max_upload_mb} MB.",
        )
    if target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="That file is empty.")

    try:
        return ingest.ingest_file(target, display)
    except LoaderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/url", status_code=201)
def add_url(body: UrlRequest) -> dict:
    try:
        return ingest.ingest_url(body.url.strip())
    except LoaderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("")
def list_documents() -> list[dict]:
    return store.list_documents()


# Comment frames keep idle connections alive through proxies, and are how a
# vanished client gets noticed: the write fails and the stream ends.
_HEARTBEAT_SECONDS = 20


def _frame(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


@router.get("/events")
async def document_events(request: Request) -> StreamingResponse:
    """Server-sent events for the knowledge base.

    Opens with a snapshot of every document, then sends one event per change:
    {"type": "document", "document": {...}} when a row is created or its status
    moves, and {"type": "deleted", "id": "..."} when one is removed. Clients
    replace polling with this, and on reconnect the snapshot resyncs them.
    """
    async def stream():
        # Subscribed inside the generator, so the finally below always pairs
        # with it -- even if the client is gone before the first frame.
        queue = events.subscribe()
        try:
            # Ask the browser to reconnect quickly if the stream drops.
            yield "retry: 2000\n\n"
            yield _frame({"type": "snapshot", "documents": store.list_documents()})
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), _HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ": keep-alive\n\n"
                    continue
                if event is events.SHUTDOWN:
                    break
                yield _frame(event)
        finally:
            events.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{document_id}")
def get_document(document_id: str) -> dict:
    document = store.get_document(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return document


@router.delete("/{document_id}", status_code=204)
def delete_document(document_id: str) -> None:
    document = store.get_document(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")

    # Remove the saved upload too, where there was one.
    source = document.get("source")
    if source and document.get("source_type") != "url":
        # `source` is the display name; files on disk use the sanitised form.
        for path in settings.uploads_dir.glob(f"*_{_safe_name(source)}"):
            path.unlink(missing_ok=True)

    if not store.delete_document(document_id):
        raise HTTPException(status_code=404, detail="Document not found")
    events.publish({"type": "deleted", "id": document_id})


@router.post("/search")
def search(body: SearchRequest) -> dict:
    """Run retrieval without generating an answer.

    Exists for tuning and debugging: it shows exactly which chunks a question
    pulls back, with both the vector and keyword scores, which is the only
    practical way to tell a retrieval problem from a generation problem.
    """
    results = retriever.retrieve(body.query, history=[], top_k=body.top_k)
    return {
        "query": body.query,
        "count": len(results),
        "results": [
            {
                "chunk_id": r.chunk_id,
                "citation": r.citation(),
                "source_type": r.source_type,
                "dense_score": round(r.dense_score, 4) if r.dense_score else None,
                "keyword_score": round(r.keyword_score, 4) if r.keyword_score else None,
                "fused_score": round(r.score, 5),
                "is_neighbour": r.is_neighbour,
                "chars": len(r.text),
                # Generous, because this endpoint exists for judging whether a
                # chunk really contains the answer; a short preview hides it.
                "preview": r.text[:1500],
            }
            for r in results
        ],
    }
