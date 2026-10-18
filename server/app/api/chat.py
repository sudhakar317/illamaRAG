"""Streaming chat route.

The reply is streamed as Server-Sent Events. Every frame is a single JSON
object with a `type` field rather than using SSE's own `event:` lines, so the
browser side has exactly one parsing path:

    data: {"type": "session",  "session_id": "...", "title": "..."}
    data: {"type": "sources",  "sources": [...], "searched": true}
    data: {"type": "thinking", "text": "..."}
    data: {"type": "token",    "text": "..."}
    data: {"type": "done",     "content": "...", "message_id": 12, "stopped": false}
    data: {"type": "error",    "message": "..."}

Generation runs in a worker thread feeding a queue, while this async route
consumes it. That split exists for one reason: llama.cpp generation is a
blocking loop that nothing can interrupt from outside. Producing in a thread
and consuming here lets a client disconnect set a flag the generation loop
checks per token -- so Stop genuinely stops, and the generation lock is
released promptly instead of being held until max_tokens.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections.abc import AsyncIterator
from functools import partial

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.config import settings
from app.core import llm
from app.db import repository as repo
from app.rag import retriever
from app.schemas.chat import ChatRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

# Bounded, so a slow client cannot make the producer buffer a whole reply in
# memory; the producer blocks instead, which is the behaviour we want.
_QUEUE_SIZE = 512

# How long to wait on the queue before re-checking whether the client is gone.
_POLL_SECONDS = 0.25


def _sse(payload: dict) -> str:
    # ensure_ascii keeps multi-byte characters out of the wire format, so a
    # frame can never be split mid-character by the transport.
    return f"data: {json.dumps(payload, ensure_ascii=True)}\n\n"


@router.post("/stream")
async def chat_stream(body: ChatRequest, request: Request) -> StreamingResponse:
    message = body.message.strip()

    # Resolve the session *before* building the generator. Everything below
    # closes over `session_id`, so the assistant reply can never be written to
    # a different session than the user message it answers.
    session_id = body.session_id
    created = False
    if not session_id or not repo.session_exists(session_id):
        session = repo.create_session(system_prompt=settings.system_prompt)
        session_id = session["id"]
        created = True

    # Persist the user's message first: if generation fails, or the user
    # navigates away mid-reply, their own message is still in the transcript.
    repo.add_message(session_id, "user", message)
    repo.autotitle_if_needed(session_id, message)

    history = repo.get_history_for_model(session_id)
    # get_history_for_model includes the message we just stored; the model gets
    # it as the current turn instead, so drop the trailing duplicate.
    if history and history[-1]["role"] == "user":
        history = history[:-1]

    title = (repo.get_session(session_id) or {}).get("title", "")

    # Retrieval runs here rather than inside the generator, so a failure
    # surfaces as a normal error instead of halfway through a stream. It is
    # fast -- one small embedding plus two index lookups -- next to generation.
    context = ""
    sources: list[dict] = []
    searched = False
    if body.use_rag:
        try:
            results = retriever.retrieve(message, history=history)
            searched = True
            if results:
                context, sources = retriever.build_context(results)
                logger.info(
                    "Retrieved %d chunk(s) for session %s", len(results), session_id
                )
        except Exception:  # noqa: BLE001 - retrieval must never break chat
            logger.exception("Retrieval failed; answering without context")

    async def event_generator() -> AsyncIterator[str]:
        events: queue.Queue = queue.Queue(maxsize=_QUEUE_SIZE)
        stop_event = threading.Event()
        pieces: list[str] = []

        def produce() -> None:
            """Run the blocking generation, pushing events onto the queue."""
            try:
                for kind, text in llm.stream_reply(
                    history=history,
                    user_message=message,
                    system_prompt=settings.system_prompt,
                    should_stop=stop_event.is_set,
                    context=context or None,
                ):
                    events.put((kind, text))
            except Exception as exc:  # noqa: BLE001 - surfaced as an SSE event
                logger.exception("Generation failed for session %s", session_id)
                events.put(("error", str(exc)))
            finally:
                events.put(None)  # sentinel: nothing more is coming

        worker = threading.Thread(
            target=produce, name=f"generate-{session_id[:8]}", daemon=True
        )
        worker.start()

        try:
            yield _sse(
                {
                    "type": "session",
                    "session_id": session_id,
                    "title": title,
                    "created": created,
                }
            )

            # Sent before the first token, so citations are on screen while
            # the answer is still being written.
            if searched:
                yield _sse({"type": "sources", "sources": sources, "searched": True})

            while True:
                try:
                    # A short timeout rather than a blocking get, so this
                    # coroutine stays cancellable and can notice a disconnect.
                    item = await anyio.to_thread.run_sync(
                        partial(events.get, True, _POLL_SECONDS)
                    )
                except queue.Empty:
                    if await request.is_disconnected():
                        stop_event.set()
                        break
                    continue

                if item is None:
                    break

                kind, text = item
                if kind == "error":
                    yield _sse({"type": "error", "message": text})
                    break

                if kind == "token":
                    pieces.append(text)
                yield _sse({"type": kind, "text": text})

            answer = "".join(pieces).strip()
            if answer:
                message_id = repo.add_message(
                    session_id, "assistant", answer, sources=sources or None
                )
                yield _sse(
                    {
                        "type": "done",
                        "content": answer,
                        "message_id": message_id,
                        "sources": sources,
                        "stopped": stop_event.is_set(),
                    }
                )

        finally:
            # Reached on success, on error, and when the client disconnects
            # (which cancels this coroutine and closes the generator). Setting
            # the flag is what actually stops llama.cpp and frees the lock.
            stop_event.set()

            if worker.is_alive():
                # One token's worth of grace so the loop can notice the flag
                # and exit cleanly; it is a daemon thread either way.
                worker.join(timeout=5.0)

            # If the client vanished before `done` could be sent, the reply is
            # still worth keeping -- it is what they saw on screen.
            if pieces and not _already_saved(session_id, pieces):
                repo.add_message(
                    session_id,
                    "assistant",
                    "".join(pieces).strip(),
                    sources=sources or None,
                )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Tells any reverse proxy not to buffer, which would defeat streaming.
            "X-Accel-Buffering": "no",
        },
    )


def _already_saved(session_id: str, pieces: list[str]) -> bool:
    """True if this reply was already written by the normal path.

    Guards the disconnect fallback above against writing a duplicate when the
    `done` branch has already stored the message.
    """
    answer = "".join(pieces).strip()
    if not answer:
        return True
    messages = repo.get_messages(session_id)
    if not messages:
        return False
    last = messages[-1]
    return last["role"] == "assistant" and last["content"] == answer
