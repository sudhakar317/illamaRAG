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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("illamarag")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("Model: %s", settings.model_path)
    if not settings.model_path.exists():
        logger.error("Model file is MISSING -- chat will fail until it is in place.")
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
from app.api.sessions import router as sessions_router  # noqa: E402

app.include_router(sessions_router)
app.include_router(chat_router)


@app.get("/api/health", tags=["health"])
def health() -> dict:
    """Model, memory and storage status. Never triggers a model load."""
    return {
        "status": "ok",
        "model": llm.model_info(),
        "memory": memory.snapshot(),
        "sessions": repo.count_sessions(),
    }
