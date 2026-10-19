"""Schema-constrained model calls for the agent's structured steps.

Every step that is not the final answer goes through `call_json`: the prompt
is rendered as ChatML, generation is constrained by a grammar compiled from a
JSON schema, and the result is parsed. A grammar makes a malformed reply
impossible rather than merely unlikely; the one way it can still fail is by
running out of tokens mid-object, which is retried once with more room.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from functools import lru_cache

from llama_cpp import LlamaGrammar

from app.config import settings
from app.core import llm
from app.core.prompt import format_chatml

logger = logging.getLogger(__name__)


class AgentError(Exception):
    """A step failed in a way the user should see as a readable message."""


@lru_cache(maxsize=32)
def _grammar(schema_json: str) -> LlamaGrammar:
    # Compiling a grammar takes noticeable time, and each step reuses one schema.
    return LlamaGrammar.from_json_schema(schema_json, verbose=False)


def _fit(user: str, system: str, max_tokens: int) -> str:
    """Trim the user block from the front until the prompt fits the window.

    The front goes first because every agent prompt puts its instructions and
    the item being judged at the end, and bulky passages earlier.
    """
    budget = settings.n_ctx - max_tokens - 32
    while True:
        prompt = format_chatml(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            enable_thinking=False,
        )
        n_tokens = llm.count_tokens(prompt)
        if n_tokens <= budget or len(user) < 400:
            return prompt
        keep = int(len(user) * budget / n_tokens * 0.9)
        user = "...(trimmed)\n" + user[-keep:]


def call_json(
    system: str,
    user: str,
    schema: dict,
    *,
    max_tokens: int = 512,
    should_stop: Callable[[], bool] | None = None,
    on_token: Callable[[int], None] | None = None,
) -> dict:
    # Never sort the keys: the grammar emits properties in the order given,
    # and that order is deliberate (decide before writing prose).
    grammar = _grammar(json.dumps(schema))
    for attempt, tokens in enumerate((max_tokens, max_tokens * 2)):
        prompt = _fit(user, system, tokens)
        raw = llm.generate_constrained(
            prompt, grammar, max_tokens=tokens, should_stop=should_stop, on_token=on_token
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "Constrained output did not parse (attempt %d, %d chars); %s",
                attempt + 1,
                len(raw),
                "retrying with more room" if attempt == 0 else "giving up",
            )
    raise AgentError("The model could not produce a valid step. Try rephrasing.")
