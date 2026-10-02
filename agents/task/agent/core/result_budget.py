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

``TOOL_RESULT_MAX_TOKENS`` defaults to 6000 (the prod value) since 2026-09-22;
``0`` = off, byte-identical.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: chars per token — the same order every provider bills and the same estimate
#: `tools/filesystem.py` and the schema-token gauge already use.
_CHARS_PER_TOKEN = 4

#: the documented prod value; one number, read by the flag row and the tests.
DEFAULT_TOOL_RESULT_MAX_TOKENS = 6000


def tool_result_max_tokens() -> int:
    """``TOOL_RESULT_MAX_TOKENS`` — default ``DEFAULT_TOOL_RESULT_MAX_TOKENS``;
    ``0`` = no cap."""
    from core.env import int_env
    value = int_env("TOOL_RESULT_MAX_TOKENS", DEFAULT_TOOL_RESULT_MAX_TOKENS)
    return value if value > 0 else 0


def named_truncation(
    content: str,
    budget_tokens: int,
    *,
    way_out: str,
    keep_tail_ratio: float = 0.0,
    keep_head_ratio: float | None = None,
    label: str = "",
) -> str:
    """Cut *content* to *budget_tokens* and NAME the cut, with the way out.

    This is the shared shape behind every honest truncation in the tree (F25).
    A silent cut makes the model believe it read the whole thing, which is the
    same class of defect as a partial screen rendering as "no risk flags raised".

    - ``keep_tail_ratio > 0`` keeps a TAIL as well as a head, with the marker
      between them, so the end of a document (where a project file usually puts
      its landmines) is not the part that disappears.
    - ``keep_head_ratio`` defaults to ``1 - keep_tail_ratio``. Pass it to reserve
      headroom: the project-context caller uses 0.70 head / 0.20 tail, a reference agent's
      split, leaving 10 % of the budget for the marker and slack.
    - ``way_out`` is the sentence that tells the model how to get the rest
      (``use offset/limit to read more``, ``read <path> for the full file``).
    - ``label`` names WHAT was cut; empty for a tool result.

    A non-str, an empty string, a non-positive budget, or content already within
    budget is returned unchanged (identity, not a copy).
    """
    if not isinstance(content, str) or not content:
        return content
    if budget_tokens <= 0:
        return content
    limit_chars = budget_tokens * _CHARS_PER_TOKEN
    if len(content) <= limit_chars:
        return content
    total_tokens = max(1, len(content) // _CHARS_PER_TOKEN)
    tail_ratio = max(0.0, min(0.9, keep_tail_ratio))
    if keep_head_ratio is None:
        # Exact integer split: no float rounding on the head-only path, so the
        # tool-result bytes stay identical to the pre-F25 output.
        tail_chars = int(limit_chars * tail_ratio)
        head_chars = limit_chars - tail_chars
    else:
        head_chars = int(limit_chars * max(0.0, min(1.0, keep_head_ratio)))
        tail_chars = int(limit_chars * tail_ratio)
    head = content[:head_chars]
    if tail_chars <= 0:
        return (
            f"{head}\n[…{label}truncated: {budget_tokens:,} of {total_tokens:,} "
            f"tokens; {way_out}]"
        )
    tail = content[-tail_chars:]
    kept_tokens = max(1, (head_chars + tail_chars) // _CHARS_PER_TOKEN)
    return (
        f"{head}\n[…{label}truncated: kept {kept_tokens:,} of {total_tokens:,} "
        f"tokens (head + tail); {way_out}]\n{tail}"
    )


def truncate_tool_result(content: str, max_tokens: int = -1) -> str:
    """Return *content*, cut to *max_tokens* with an honest tail.

    *max_tokens* defaults to reading the env. A non-str, an empty string, or a
    value already within budget is returned unchanged (identity, not a copy).
    Head-only, byte-identical to the pre-F25 output — this is the one call that
    pins :func:`named_truncation`'s no-tail branch.
    """
    if not isinstance(content, str) or not content:
        return content
    budget = tool_result_max_tokens() if max_tokens < 0 else max_tokens
    return named_truncation(
        content, budget, way_out="use offset/limit to read more"
    )
