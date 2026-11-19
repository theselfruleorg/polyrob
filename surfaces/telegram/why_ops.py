"""`/why` — the last refusals, in the gate's own words (O10).

The typed gate refusals (``core/security/refusals.py``: money gate, approval
denial or timeout, forged turn, pause, correspondent taint …) were counted on
`/status` and listed nowhere. The owner learned "why" only from the agent's
paraphrase in the same turn. This verb prints each reason VERBATIM (trimmed), so
the answer does not depend on the model.

The read and the text are ``core.security_digest.refusals_reply`` — the agent's
``agent_status(view="refusals")`` prints the same (120 W3). This module only
parses the verb's words. Shared: the REPL imports :func:`why_reply`.
"""
from __future__ import annotations

from typing import List, Optional

USAGE = (
    "Usage: /why [n]  — the last n refusals (default 5, at most 20)\n"
    "Each line: when, which tool, the gate's reason, word for word."
)


def why_reply(user_id: Optional[str], data_dir: str, args: List[str]) -> str:
    from core.security_digest import REFUSALS_DEFAULT_N, refusals_reply
    n = REFUSALS_DEFAULT_N
    if args:
        try:
            n = int(args[0])
        except ValueError:
            return USAGE
        if n < 1:
            return USAGE
    return refusals_reply(user_id or "", data_dir, n)


__all__ = ["USAGE", "why_reply"]
