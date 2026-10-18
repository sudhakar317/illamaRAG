"""Prompt construction for Qwen3.5 in ChatML format, plus history trimming.

We format the prompt by hand rather than using the GGUF's embedded Jinja chat
template. That template is the *multimodal* Qwen3.5 one -- it uses Jinja
`namespace` objects and macros to count images and videos -- which is a lot of
machinery for a text-only chat, and it hides the final prompt from us. Writing
ChatML directly keeps the exact prompt visible and easy to extend later (RAG
context goes in as one more block).
"""

from collections.abc import Callable

IM_START = "<|im_start|>"
IM_END = "<|im_end|>"

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"

# Generation stops at the end of the assistant's turn. Both are included
# because a model will occasionally open a new turn instead of closing its own.
STOP_SEQUENCES = [IM_END, IM_START]


def format_chatml(messages: list[dict], enable_thinking: bool = False) -> str:
    """Render messages as a ChatML prompt ending in an open assistant turn.

    When thinking is disabled we prefill an empty `<think></think>` block. That
    is the documented Qwen3.x convention for skipping the reasoning phase: the
    model sees reasoning as already finished and goes straight to the answer.
    """
    parts = [f"{IM_START}{m['role']}\n{m['content']}{IM_END}\n" for m in messages]
    parts.append(f"{IM_START}assistant\n")
    if not enable_thinking:
        parts.append(f"{THINK_OPEN}\n\n{THINK_CLOSE}\n\n")
    return "".join(parts)


def build_messages(
    system_prompt: str,
    history: list[dict],
    user_message: str,
) -> list[dict]:
    """Assemble the message list sent to the model.

    This is the single seam where retrieved context will be injected when RAG is
    added later -- routes, storage and client all stay unchanged.
    """
    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.extend({"role": m["role"], "content": m["content"]} for m in history)
    messages.append({"role": "user", "content": user_message})
    return messages


def trim_to_budget(
    system_prompt: str,
    history: list[dict],
    user_message: str,
    count_tokens: Callable[[str], int],
    budget: int,
    enable_thinking: bool = False,
) -> tuple[str, int, int]:
    """Drop the oldest turns until the rendered prompt fits inside `budget`.

    Trimming is measured by actually tokenizing the candidate prompt rather than
    by keeping "the last N messages": a fixed message count either wastes most of
    the window on short turns or silently overflows it on long ones. The system
    prompt and the current user message are never dropped.

    Returns (prompt, prompt_tokens, dropped_message_count).
    """
    kept = list(history)
    dropped = 0

    while True:
        messages = build_messages(system_prompt, kept, user_message)
        prompt = format_chatml(messages, enable_thinking)
        n_tokens = count_tokens(prompt)

        if n_tokens <= budget:
            return prompt, n_tokens, dropped

        if kept:
            kept.pop(0)
            dropped += 1
            continue

        # No history left and still over budget: the user's own message is too
        # long for the context window. Truncate it from the front, keeping the
        # end, which is where a question usually is.
        overflow_ratio = budget / max(n_tokens, 1)
        keep_chars = max(200, int(len(user_message) * overflow_ratio * 0.9))
        if keep_chars >= len(user_message):
            # Cannot shrink any further; hand it back and let the caller surface
            # the model's own context error rather than looping forever.
            return prompt, n_tokens, dropped
        user_message = "...(truncated)\n" + user_message[-keep_chars:]


class ThinkSplitter:
    """Separates `<think>...</think>` reasoning from the visible answer, mid-stream.

    Tokens arrive in arbitrary pieces, so a tag can be split across two chunks
    ("<thi" then "nk>"). This buffers only a tail that is a genuine prefix of the
    tag currently being looked for, so output is held back for as short a time as
    possible while never missing a split tag.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._in_think = False

    @property
    def _kind(self) -> str:
        return "thinking" if self._in_think else "token"

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        self._buf += chunk
        out: list[tuple[str, str]] = []

        while True:
            tag = THINK_CLOSE if self._in_think else THINK_OPEN
            idx = self._buf.find(tag)
            if idx == -1:
                break
            before = self._buf[:idx]
            if before:
                out.append((self._kind, before))
            self._buf = self._buf[idx + len(tag) :]
            self._in_think = not self._in_think

        # Hold back only a trailing partial tag.
        tag = THINK_CLOSE if self._in_think else THINK_OPEN
        hold = 0
        for size in range(min(len(tag) - 1, len(self._buf)), 0, -1):
            if self._buf.endswith(tag[:size]):
                hold = size
                break

        split = len(self._buf) - hold
        emit, self._buf = self._buf[:split], self._buf[split:]
        if emit:
            out.append((self._kind, emit))
        return out

    def flush(self) -> list[tuple[str, str]]:
        """Emit whatever is left once the stream ends."""
        if not self._buf:
            return []
        out = [(self._kind, self._buf)]
        self._buf = ""
        return out
