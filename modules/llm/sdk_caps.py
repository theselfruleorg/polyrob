"""What the INSTALLED provider SDK actually accepts.

``requirements.txt`` pins ``openai>=1.26.0``, but ``prompt_cache_key`` — a pure
prefix-cache OPTIMISATION — arrived in the SDK much later. On an install that
resolves to an older-but-permitted version every request raised
``TypeError: AsyncCompletions.create() got an unexpected keyword argument
'prompt_cache_key'`` and the agent reported "All LLM providers failed": a total
outage caused by a cost optimisation, inside a version range we ourselves
allow. Reproduced on openai 1.68.2.

The rule this module exists for: **a parameter we send to SAVE money must never
be able to kill the call.** A parameter that changes the ANSWER is out of
scope — that one should fail loudly.

Its own module rather than a helper inside ``openai_client.py``: that file is
under a shrink-only size ratchet, and every OpenAI-compatible client
(openrouter, deepseek, nim) can hit the same class.
"""
from __future__ import annotations

import inspect
from functools import lru_cache


@lru_cache(maxsize=None)
def sdk_supports(param: str) -> bool:
    """Does the installed openai SDK accept *param* on ``chat.completions.create``?

    Fail-OPEN (True) when the signature cannot be read, so an SDK we cannot
    introspect keeps today's behaviour rather than silently losing the
    optimisation for everyone. A ``**kwargs`` signature is also True: it accepts
    anything, and the server decides.
    """
    try:
        from openai.resources.chat.completions import AsyncCompletions
        sig = inspect.signature(AsyncCompletions.create)
    except Exception:
        return True
    params = sig.parameters
    if any(p.kind is p.VAR_KEYWORD for p in params.values()):
        return True
    return param in params
