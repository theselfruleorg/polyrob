"""Per-session OUTPUT budget, and 'was the last answer cut off?'.

**Latency is output-bound.** A 24 h prod measurement: ≤1k output
tokens → 13 s, 5k → 101 s, 16k → 272 s, while UNCACHED INPUT was flat at 41-63 s
across every bucket. Five calls a day hit the 16,384-token wire ceiling and ran
172-330 s each — against a 600 s cron cap.

Two things live here, both keyed off the same fact:

1. :func:`output_token_cap` — the ONE answer to "how many output tokens may this
   request ask for". It is ``LLM_MAX_OUTPUT_TOKENS`` (the existing absolute
   ceiling: cost + the credit pre-authorisation a metered provider performs),
   narrowed by ``AUTONOMOUS_MAX_OUTPUT_TOKENS`` for a cron/goal session's task
   (``agents.task.session_class.apply_session_output_budget``). An owner's chat keeps the
   old ceiling.
2. :func:`finish_reason_of` / :func:`output_was_truncated` — whether the provider
   stopped because it ran out of budget. Nothing in the tree surfaced this
   before, so a cut-off answer arrived as malformed JSON and was "repaired" into
   a guess. A budget with no truncation signal is worse than no budget.

Defaults: ``AUTONOMOUS_MAX_OUTPUT_TOKENS=0`` = off, byte-identical.
"""
from __future__ import annotations

import logging
from modules.llm.call_scope import call_output_cap, session_output_cap

logger = logging.getLogger(__name__)

def autonomous_max_output_tokens() -> int:
    """``AUTONOMOUS_MAX_OUTPUT_TOKENS`` — 0 (default) = off."""
    from core.env import int_env
    value = int_env("AUTONOMOUS_MAX_OUTPUT_TOKENS", 0)
    return value if value > 0 else 0


def output_token_cap(default_cap: int) -> int:
    """The per-request output ceiling: ``LLM_MAX_OUTPUT_TOKENS`` narrowed by this
    session's autonomous budget, if one was stamped, and by a
    :func:`bounded_output` scope."""
    from core.env import int_env
    cap = int_env("LLM_MAX_OUTPUT_TOKENS", default_cap)
    if cap <= 0:
        cap = default_cap
    narrowed = call_output_cap()
    if narrowed > 0:
        cap = min(cap, narrowed)
    session_cap = session_output_cap()
    if session_cap > 0:
        return min(cap, session_cap)
    return cap
