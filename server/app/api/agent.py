"""Agent routes: run the claim tree, answer its questions, approve what it keeps.

POST /api/agent/stream starts a run from a message, or resumes one with the
user's answers. Events are SSE frames in the same one-JSON-object shape as
chat, so the client keeps one parser:

    data: {"type": "session",   "session_id": "...", "title": "...", "created": false}
    data: {"type": "run",       "run_id": "..."}
    data: {"type": "step",      "step": {id, stage, label, status, ms, details}, "closed": {...}}
    data: {"type": "progress",  "step": 3, "tokens": 41, "seconds": 7.5}
    data: {"type": "tree",      "claims": [...]}
    data: {"type": "questions", "run_id": "...", "questions": [...], "message_id": 12}
    data: {"type": "token",     "text": "..."}
    data: {"type": "done",      "content": "...", "message_id": 13, "sources": [...]}
    data: {"type": "proposals", "items": [...], "message_id": 13}
    data: {"type": "error",     "message": "..."}

A "step" event carries the whole step each time, so the client only ever
replaces by id; "closed" is the previous step, finished, with its duration.
"progress" arrives about twice a second while a model call runs, so a step
that takes forty seconds on a CPU visibly moves instead of looking stuck.

A run that asks a question ends its stream there. Its state is saved, and the
next request carries the answers and the run id to pick it up again. Every
assistant message an agent run writes carries {"run_id", "kind"} in its meta,
which is how GET /state redraws the reasoning behind it after a reload.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections.abc import AsyncIterator
from functools import partial

import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent import model, tree
from app.agent import store as agent_store
from app.agent.claims import Run
from app.config import settings
from app.db import repository as repo
from app.rag import events, ingest, store
from app.rag.loaders import LoaderError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent"])

_QUEUE_SIZE = 512
_POLL_SECONDS = 0.25


class AgentRequest(BaseModel):
    message: str | None = Field(default=None, max_length=8000)
    session_id: str | None = None
    # Resuming: the run that asked, and an answer per claim id.
    run_id: str | None = None
    answers: dict[str, str] | None = None


class ProposalEdit(BaseModel):
    key: str | None = Field(default=None, max_length=60)
    value: str | None = Field(default=None, max_length=2000)
    title: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=8000)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=True)}\n\n"


def _questions_text(questions: list[dict]) -> str:
    """How a question turn reads in the transcript, after a reload."""
    lines = ["I need a little more to answer this properly:"]
    for i, q in enumerate(questions, start=1):
        lines.append(f"{i}. {q['text']}")
    return "\n".join(lines)


def _answers_text(run: Run, answers: dict[str, str]) -> str:
    lines = []
    for q in run.questions:
        reply = (answers.get(q["claim_id"]) or "").strip() or "I don't know"
        lines.append(f"{q['text']}\n{reply}")
    return "\n\n".join(lines)


@router.post("/stream")
async def agent_stream(body: AgentRequest, request: Request) -> StreamingResponse:
    created = False

    if body.run_id:
        run = agent_store.load_run(body.run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="That question has expired.")
        if run.status != "awaiting":
            raise HTTPException(status_code=409, detail="Those questions were already answered.")
        session_id = run.session_id
        answers = body.answers or {}
        repo.add_message(session_id, "user", _answers_text(run, answers))
        # should_stop is bound to this request's stop flag once streaming starts.
        agent = tree.Agent(run, should_stop=lambda: False)
        steps = partial(agent.resume, answers)
    else:
        message = (body.message or "").strip()
        if not message:
            raise HTTPException(status_code=422, detail="A message is required.")
        session_id = body.session_id
        if not session_id or not repo.session_exists(session_id):
            session_id = repo.create_session(system_prompt=settings.system_prompt)["id"]
            created = True
        agent_store.abandon_awaiting(session_id)
        repo.add_message(session_id, "user", message)
        repo.autotitle_if_needed(session_id, message)
        history = repo.get_history_for_model(session_id)[:-1]
        run = tree.new_run(session_id, message)
        agent_store.save_run(run)
        agent = tree.Agent(run, should_stop=lambda: False)
        steps = partial(agent.start, history)

    title = (repo.get_session(session_id) or {}).get("title", "")

    async def event_generator() -> AsyncIterator[str]:
        items: queue.Queue = queue.Queue(maxsize=_QUEUE_SIZE)
        stop_event = threading.Event()
        agent.should_stop = stop_event.is_set

        def emit(event: dict) -> None:
            # Progress is a nicety: if the client is slow to read, drop it
            # rather than stall the model mid-step.
            try:
                items.put_nowait(event)
            except queue.Full:
                pass

        agent.emit = emit
        pieces: list[str] = []
        answer_saved = False
        last_message_id: int | None = None

        def produce() -> None:
            try:
                for event in steps():
                    items.put(event)
            except tree.Cancelled:
                run.status = "stopped"
                agent_store.save_run(run)
            except model.AgentError as exc:
                run.status = "failed"
                agent_store.save_run(run)
                items.put({"type": "error", "message": str(exc)})
            except Exception as exc:  # noqa: BLE001 - surfaced as an SSE event
                logger.exception("Agent run %s failed", run.id)
                run.status = "failed"
                agent_store.save_run(run)
                items.put({"type": "error", "message": f"The agent failed: {exc}"})
            finally:
                items.put(None)

        worker = threading.Thread(target=produce, name=f"agent-{run.id[:8]}", daemon=True)
        worker.start()

        try:
            yield _sse({"type": "session", "session_id": session_id, "title": title, "created": created})
            yield _sse({"type": "run", "run_id": run.id})

            while True:
                try:
                    event = await anyio.to_thread.run_sync(
                        partial(items.get, True, _POLL_SECONDS)
                    )
                except queue.Empty:
                    if await request.is_disconnected():
                        stop_event.set()
                        break
                    continue
                if event is None:
                    break

                kind = event["type"]
                if kind == "token":
                    pieces.append(event["text"])
                    yield _sse(event)
                elif kind == "questions":
                    last_message_id = repo.add_message(
                        session_id,
                        "assistant",
                        _questions_text(event["questions"]),
                        meta={"run_id": run.id, "kind": "questions"},
                    )
                    yield _sse({**event, "message_id": last_message_id})
                elif kind == "answer":
                    last_message_id = repo.add_message(
                        session_id,
                        "assistant",
                        event["content"],
                        sources=event["sources"] or None,
                        meta={"run_id": run.id, "kind": "answer"},
                    )
                    answer_saved = True
                    yield _sse(
                        {
                            "type": "done",
                            "content": event["content"],
                            "sources": event["sources"],
                            "message_id": last_message_id,
                        }
                    )
                elif kind == "proposals":
                    yield _sse({**event, "message_id": last_message_id})
                else:
                    yield _sse(event)
        finally:
            stop_event.set()
            if worker.is_alive():
                worker.join(timeout=5.0)
            # Stopped mid-answer: keep what was on screen, as chat does.
            partial_answer = "".join(pieces).strip()
            if partial_answer and not answer_saved:
                repo.add_message(
                    session_id,
                    "assistant",
                    partial_answer,
                    meta={"run_id": run.id, "kind": "answer"},
                )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get("/state")
def agent_state(session_id: str) -> dict:
    """What a reloaded page needs to redraw the agent's side of a session.

    runs: every recent run's claim tree and steps, keyed by run id, matched
    to messages through their meta. pending: the run still waiting on the
    user, with its questions. proposals: what is still awaiting approval.
    """
    run = agent_store.awaiting_run(session_id)
    return {
        "runs": {r.id: tree.run_view(r) for r in agent_store.session_runs(session_id)},
        "pending": {"run_id": run.id, "questions": run.questions} if run else None,
        "proposals": agent_store.pending_proposals(session_id),
    }


@router.post("/proposals/{proposal_id}/save")
def save_proposal(proposal_id: str, body: ProposalEdit) -> dict:
    proposal = agent_store.get_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if proposal["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Already {proposal['status']}.")

    edits = body.model_dump(exclude_none=True)
    merged = {**proposal, **edits}

    if merged["destination"] == "profile":
        key = tree.slug(merged.get("key") or "")
        value = (merged.get("value") or "").strip()
        if not value:
            raise HTTPException(status_code=422, detail="A value is required.")
        agent_store.profile_set(key, value, source="user")
        return agent_store.resolve_proposal(proposal_id, "saved", key=key, value=value)  # type: ignore[return-value]

    title = (merged.get("title") or "").strip()
    note = (merged.get("note") or "").strip()
    # Supersede rather than accumulate: a second note on the same subject
    # replaces the first, so retrieval never has to pick between two versions.
    for old in store.find_documents(title, origin="conversation"):
        store.delete_document(old["id"])
        events.publish({"type": "deleted", "id": old["id"]})
    try:
        document = ingest.ingest_text(title, note, source="Stated in chat")
    except LoaderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return agent_store.resolve_proposal(  # type: ignore[return-value]
        proposal_id, "saved", document_id=document["id"], title=title, note=note
    )


@router.post("/proposals/{proposal_id}/discard")
def discard_proposal(proposal_id: str) -> dict:
    proposal = agent_store.get_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return agent_store.resolve_proposal(proposal_id, "discarded")  # type: ignore[return-value]


@router.get("/profile")
def list_profile() -> list[dict]:
    return agent_store.profile_list()


@router.delete("/profile/{key}", status_code=204)
def delete_profile(key: str) -> None:
    if not agent_store.profile_delete(key):
        raise HTTPException(status_code=404, detail="Not in the profile")
