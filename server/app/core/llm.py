"""Single shared llama.cpp model instance, plus streaming generation.

Two rules drive this module:

1. *One* model instance for the whole process, created lazily on first use.
   The weights are 2.74 GB; loading per request, or at import time, is the
   difference between this project running on 8 GB of RAM and not.

2. Generation is serialized by a lock. llama.cpp is not thread-safe, and
   FastAPI runs synchronous `def` routes in a worker threadpool -- so two
   concurrent chats would otherwise share one KV cache and corrupt each
   other's output. The lock is held for the whole duration of a generation,
   not just the call that starts it.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator

import llama_cpp
from llama_cpp import Llama

from app.config import settings
from app.core.prompt import (
    STOP_SEQUENCES,
    ThinkSplitter,
    format_chatml,
    trim_to_budget,
)

logger = logging.getLogger(__name__)

_llm: Llama | None = None
_load_lock = threading.Lock()

# Held for the entire duration of a generation. See rule 2 above.
_generate_lock = threading.Lock()

_KV_TYPES = {
    "q8_0": llama_cpp.GGML_TYPE_Q8_0,
    "q4_0": llama_cpp.GGML_TYPE_Q4_0,
    "f16": llama_cpp.GGML_TYPE_F16,
}


def _base_kwargs() -> dict:
    return {
        "model_path": str(settings.model_path),
        "n_ctx": settings.n_ctx,
        "n_threads": settings.n_threads,
        "n_batch": settings.n_batch,
        "n_gpu_layers": settings.n_gpu_layers,
        # mmap lets the OS page weights in on demand and evict them under
        # pressure. mlock would pin all 2.74 GB in RAM -- the single worst
        # setting to enable on an 8 GB machine.
        "use_mmap": True,
        "use_mlock": False,
        "verbose": False,
    }


def _load() -> Llama:
    path = settings.model_path
    if not path.exists():
        raise FileNotFoundError(
            f"GGUF model not found at: {path}\n"
            "Place the model there, or set MODEL_PATH in server/.env"
        )

    size_gb = path.stat().st_size / (1024**3)
    logger.info("Loading model %s (%.2f GB) ...", path.name, size_gb)

    kwargs = _base_kwargs()
    kv_type = _KV_TYPES.get(settings.kv_cache_type)

    if kv_type is not None and settings.kv_cache_type != "f16":
        # A quantized V cache requires flash attention in llama.cpp.
        try:
            model = Llama(**kwargs, type_k=kv_type, type_v=kv_type, flash_attn=True)
            logger.info("Model loaded (KV cache: %s).", settings.kv_cache_type)
            return model
        except Exception as exc:  # noqa: BLE001 - fall back rather than fail
            logger.warning(
                "Quantized KV cache (%s) unavailable (%s). "
                "Falling back to the f16 cache, which uses about twice the memory.",
                settings.kv_cache_type,
                exc,
            )

    model = Llama(**kwargs)
    logger.info("Model loaded (KV cache: f16).")
    return model


def get_llm() -> Llama:
    """Return the shared model, loading it on first call."""
    global _llm
    if _llm is None:
        with _load_lock:
            if _llm is None:  # re-check: another thread may have won the race
                _llm = _load()
    return _llm


def is_loaded() -> bool:
    """True if the model is already in memory (does not trigger a load)."""
    return _llm is not None


def count_tokens(text: str) -> int:
    """Token count for `text`, counting ChatML control tokens as the model does."""
    llm = get_llm()
    return len(llm.tokenize(text.encode("utf-8"), add_bos=False, special=True))


def prompt_budget() -> int:
    """Tokens available for the prompt, once room for the reply is reserved."""
    # 64 tokens of slack absorbs small differences between our rendering of the
    # prompt and what the model ends up tokenizing.
    return max(256, settings.n_ctx - settings.max_output_tokens - 64)


def stream_reply(
    history: list[dict],
    user_message: str,
    system_prompt: str | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> Iterator[tuple[str, str]]:
    """Stream a reply, yielding ("token" | "thinking", text) pairs.

    Reasoning blocks are separated from the answer here so that callers never
    have to deal with raw `<think>` tags.

    `should_stop` is polled once per token. It is what makes cancellation real:
    llama.cpp generation is a blocking loop inside a worker thread, and nothing
    outside that thread can interrupt it. Without this check, a client that
    disconnects (Stop, or closing the tab) leaves the model generating to
    max_tokens while still holding the generation lock -- which blocks every
    other chat for as long as it takes to finish a reply nobody will read.
    """
    llm = get_llm()
    system_prompt = settings.system_prompt if system_prompt is None else system_prompt

    prompt, n_prompt_tokens, dropped = trim_to_budget(
        system_prompt=system_prompt,
        history=history,
        user_message=user_message,
        count_tokens=count_tokens,
        budget=prompt_budget(),
        enable_thinking=settings.enable_thinking,
    )
    if dropped:
        logger.info("Trimmed %d oldest message(s) to fit the context window.", dropped)
    logger.info("Prompt: %d tokens (budget %d).", n_prompt_tokens, prompt_budget())

    splitter = ThinkSplitter()

    # The lock spans the generator body, so it is released when the consumer
    # finishes *or* abandons the generator (client disconnect raises
    # GeneratorExit here, which unwinds the `with`).
    with _generate_lock:
        stream = llm.create_completion(
            prompt=prompt,
            max_tokens=settings.max_output_tokens,
            temperature=settings.temperature,
            top_p=settings.top_p,
            repeat_penalty=settings.repeat_penalty,
            stop=STOP_SEQUENCES,
            stream=True,
        )
        try:
            for chunk in stream:
                if should_stop is not None and should_stop():
                    logger.info("Generation cancelled by the client.")
                    break
                piece = chunk["choices"][0]["text"]
                if not piece:
                    continue
                yield from splitter.feed(piece)
            yield from splitter.flush()
        finally:
            # Close llama.cpp's own generator explicitly rather than leaving it
            # to the garbage collector, so its resources are freed before the
            # lock is released and the next request starts.
            stream.close()


def model_info() -> dict:
    """Describe the configured model without forcing it to load."""
    path = settings.model_path
    return {
        "name": path.name,
        "path": str(path),
        "exists": path.exists(),
        "size_gb": round(path.stat().st_size / (1024**3), 2) if path.exists() else None,
        "loaded": is_loaded(),
        "n_ctx": settings.n_ctx,
        "n_threads": settings.n_threads,
        "kv_cache_type": settings.kv_cache_type,
        "max_output_tokens": settings.max_output_tokens,
        "thinking_enabled": settings.enable_thinking,
    }
