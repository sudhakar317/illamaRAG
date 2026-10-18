"""FastAPI application entry point.

Run from the server/ directory:
    .venv/Scripts/python.exe -m uvicorn app.main:app --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.core import llm, memory
from app.db import repository as repo
from app.db.database import init_db
from app.rag import embedder, events, store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("illamarag")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    store.init_rag_db()
    # Lets Ctrl+C end open document event streams instead of waiting on them.
    events.install_signal_hook()

    logger.info("Chat model : %s", settings.model_path)
    logger.info("Embed model: %s", settings.embed_model_path)
    if not settings.model_path.exists():
        logger.error("Chat model is MISSING -- chat will fail until it is in place.")
    if not settings.embed_model_path.exists():
        logger.warning(
            "Embedding model is MISSING -- uploads and retrieval will fail. "
            "Chat still works."
        )
    # The model itself is loaded on the first chat request, not here: startup
    # stays instant, and 2.5 GB is only committed once it is actually needed.
    logger.info("Ready on http://127.0.0.1:8000  (model loads on first message)")
    yield


app = FastAPI(
    title="illamaRAG",
    description="Local chatbot with persistent chats and sessions, powered by a Qwen GGUF model via llama.cpp.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    # Explicit origins, not ["*"]: the wildcard is invalid in combination with
    # allow_credentials=True and browsers reject the response.
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Imported after `app` exists purely to keep the import graph obvious.
from app.api.chat import router as chat_router  # noqa: E402
from app.api.documents import router as documents_router  # noqa: E402
from app.api.sessions import router as sessions_router  # noqa: E402

app.include_router(sessions_router)
app.include_router(chat_router)
app.include_router(documents_router)


@app.get("/api/health", tags=["health"])
def health() -> dict:
    """Model, memory and storage status. Never triggers a model load."""
    document_count, chunk_count = store.count_documents()
    return {
        "status": "ok",
        "model": llm.model_info(),
        "embedder": embedder.model_info(),
        "memory": memory.snapshot(),
        "sessions": repo.count_sessions(),
        "knowledge": {"documents": document_count, "chunks": chunk_count},
    }
