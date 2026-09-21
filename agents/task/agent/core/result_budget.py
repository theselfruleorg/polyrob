"""Cap the size of ONE tool result on the wire — 057 WS-B.

The prod measurement behind this: 80% of input tokens are served from cache,
so the tool schemas cost ~$0.25/day. The bulk of the uncached spend is the
per-step SUFFIX — ~25k tokens a call, most of it tool RESULTS — because
``read_file`` has ``offset``/``limit`` but no default page, and a single
``grep``/``web_fetch``/MCP answer can be tens of thousands of tokens.

Truncation rules, deliberately narrow:

- Only the STRING that goes into the ``ToolMessage`` is cut. The ``ActionResult``
  itself is never mutated — exactly the UP-06 untrusted-wrap pattern — so memory
  previews, telemetry and artifact records keep the full content.
- The cut is NAMED, with the numbers and the way out:
  ``[…truncated: N of M tokens; use offset/limit to read more]``. A silent cut
  would make the model believe it had read a whole file, which is the same class
  as a partial screen rendering as "no risk flags raised".
- It runs BEFORE the untrusted wrap, so the closing delimiter is never the thing
  that gets cut off.

``TOOL_RESULT_MAX_TOKENS=0`` (default) = off, byte-identical.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: chars per token — the same order every provider bills and the same estimate
#: `tools/filesystem.py` and the schema-token gauge already use.
_CHARS_PER_TOKEN = 4


def tool_result_max_tokens() -> int:
    """``TOOL_RESULT_MAX_TOKENS`` — 0 (default) = no cap."""
    from core.env import int_env
    value = int_env("TOOL_RESULT_MAX_TOKENS", 0)
    return value if value > 0 else 0


def truncate_tool_result(content: str, max_tokens: int = -1) -> str:
    """Return *content*, cut to *max_tokens* with an honest tail.

    *max_tokens* defaults to reading the env. A non-str, an empty string, or a
    value already within budget is returned unchanged (identity, not a copy).
    """
    if not isinstance(content, str) or not content:
        return content
    budget = tool_result_max_tokens() if max_tokens < 0 else max_tokens
    if budget <= 0:
        return content
    limit_chars = budget * _CHARS_PER_TOKEN
    if len(content) <= limit_chars:
        return content
    total_tokens = max(1, len(content) // _CHARS_PER_TOKEN)
    head = content[:limit_chars]
    return (
        f"{head}\n[…truncated: {budget:,} of {total_tokens:,} tokens; "
        f"use offset/limit to read more]"
    )
