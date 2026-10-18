"""Text embeddings via nomic-embed-text-v1.5 running in llama.cpp.

Same discipline as core/llm.py: one lazily-created instance for the whole
process, guarded by its own lock, because llama.cpp is not thread-safe and
FastAPI serves sync routes from a threadpool.

Three details about this model are easy to get wrong and quietly ruin
retrieval rather than raising an error:

1. **Task prefixes.** nomic-embed is trained with instruction prefixes and
   expects `search_query:` on questions and `search_document:` on indexed
   text. Embedding both sides the same way measurably degrades retrieval —
   and nothing warns you, the results are just worse.
2. **Mean pooling.** The GGUF declares `pooling_type=1` (MEAN), but we pass it
   explicitly rather than relying on metadata inference.
3. **L2 normalisation.** v1.5 vectors are meant to be normalised before a
   cosine comparison, which also lets a dot product stand in for cosine.
"""

from __future__ import annotations

import logging
import math
import threading

import llama_cpp
from llama_cpp import Llama

from app.config import settings

logger = logging.getLogger(__name__)

# Prefixes defined by the model authors; see the module docstring.
QUERY_PREFIX = "search_query: "
DOCUMENT_PREFIX = "search_document: "

_model: Llama | None = None
_load_lock = threading.Lock()
_embed_lock = threading.Lock()


def _load() -> Llama:
    path = settings.embed_model_path
    if not path.exists():
        raise FileNotFoundError(
            f"Embedding model not found at: {path}\n"
            "Download nomic-embed-text-v1.5.Q8_0.gguf into models/, or set "
            "EMBED_MODEL_PATH in server/.env"
        )

    logger.info(
        "Loading embedding model %s (%.0f MB) ...",
        path.name,
        path.stat().st_size / (1024**2),
    )
    model = Llama(
        model_path=str(path),
        embedding=True,
        pooling_type=llama_cpp.LLAMA_POOLING_TYPE_MEAN,
        n_ctx=settings.embed_n_ctx,
        # Prompt-processing batch must be at least n_ctx for embeddings:
        # the whole text is encoded in a single forward pass, so a smaller
        # batch silently truncates long chunks.
        n_batch=settings.embed_n_ctx,
        n_ubatch=settings.embed_n_ctx,
        n_threads=settings.n_threads,
        n_gpu_layers=0,
        use_mmap=True,
        use_mlock=False,
        verbose=False,
    )
    logger.info("Embedding model loaded (768-dim, mean pooling).")
    return model


def get_model() -> Llama:
    global _model
    if _model is None:
        with _load_lock:
            if _model is None:
                _model = _load()
    return _model


def is_loaded() -> bool:
    return _model is not None


def _normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0:
        return vector
    return [v / norm for v in vector]


def _embed_raw(texts: list[str]) -> list[list[float]]:
    model = get_model()
    with _embed_lock:
        out = model.embed(texts)

    # llama-cpp-python returns a flat vector for a single input and a list of
    # vectors for a batch; normalise that difference away here.
    if out and isinstance(out[0], (int, float)):
        out = [out]
    return [_normalise(list(v)) for v in out]


def embed_documents(texts: list[str]) -> list[list[float]]:
    """Embed passages for indexing."""
    if not texts:
        return []
    return _embed_raw([DOCUMENT_PREFIX + t.replace("\n", " ").strip() for t in texts])


def embed_query(text: str) -> list[float]:
    """Embed a search query."""
    return _embed_raw([QUERY_PREFIX + text.replace("\n", " ").strip()])[0]


def dimensions() -> int:
    return int(get_model().n_embd())


def model_info() -> dict:
    path = settings.embed_model_path
    return {
        "name": path.name,
        "exists": path.exists(),
        "size_mb": round(path.stat().st_size / (1024**2), 1) if path.exists() else None,
        "loaded": is_loaded(),
        "n_ctx": settings.embed_n_ctx,
    }
