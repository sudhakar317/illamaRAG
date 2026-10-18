"""Standalone check that the GGUF model loads and generates on this machine.

Run from the server/ directory:
    .venv/Scripts/python.exe scripts/smoke_test.py

Everything else in the project depends on this passing, so it is worth being
able to run it on its own -- without FastAPI, the database or the client.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.core import llm, memory  # noqa: E402


def rss_mb() -> float:
    """Resident memory of this process, in MB."""
    info = memory.process_memory_mb()
    return info["rss_mb"] if info else float("nan")


def main() -> int:
    print("=" * 62)
    print("  llamaRAG smoke test")
    print("=" * 62)
    print(f"model      : {settings.model_path.name}")
    print(f"n_ctx      : {settings.n_ctx}")
    print(f"n_threads  : {settings.n_threads}")
    print(f"kv cache   : {settings.kv_cache_type}")
    print(f"thinking   : {settings.enable_thinking}")
    print(f"RSS before : {rss_mb():.0f} MB")
    print("-" * 62)

    t0 = time.perf_counter()
    llm.get_llm()
    load_s = time.perf_counter() - t0
    print(f"model loaded in {load_s:.1f}s   RSS now: {rss_mb():.0f} MB")
    print("-" * 62)

    question = "In one short sentence, what is a vector database?"
    print(f"Q: {question}\nA: ", end="", flush=True)

    n_tokens = 0
    thinking = []
    t0 = time.perf_counter()
    for kind, text in llm.stream_reply(history=[], user_message=question):
        if kind == "token":
            print(text, end="", flush=True)
            n_tokens += 1
        else:
            thinking.append(text)
    elapsed = time.perf_counter() - t0

    print("\n" + "-" * 62)
    if thinking:
        print(f"thinking block: {len(''.join(thinking))} chars (hidden from the answer)")
    rate = n_tokens / elapsed if elapsed else 0
    print(f"{n_tokens} chunks in {elapsed:.1f}s  ->  ~{rate:.1f} chunks/sec")
    print(f"RSS peak   : {rss_mb():.0f} MB")
    print("-" * 62)

    # A reply of zero tokens means the prompt format is wrong -- the model hit a
    # stop sequence immediately -- which is the failure worth catching here.
    if n_tokens == 0:
        print("FAIL: model produced no output. Check the ChatML prompt format.")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
