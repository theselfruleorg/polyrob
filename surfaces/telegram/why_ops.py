"""`/why` — the last refusals, in the gate's own words (O10).

The typed gate refusals (``core/security/refusals.py``: money gate, approval
denial or timeout, forged turn, pause, correspondent taint …) were counted on
`/status` and listed nowhere. The owner learned "why" only from the agent's
paraphrase in the same turn. This verb reads the rows and prints each reason
VERBATIM (trimmed), so the answer does not depend on the model.

Read-only: it opens the telemetry store and writes nothing; an absent or
unreadable store answers ``unavailable(<reason>)``, never "no refusals".

Shared: the REPL imports :func:`why_reply`.
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_N = 5
_MAX_N = 20
_DETAIL_CHARS = 180

USAGE = (
    "Usage: /why [n]  — the last n refusals (default 5, at most 20)\n"
    "Each line: when, which tool, the gate's reason, word for word."
)


def _when(ts: float, now: float) -> str:
    age = max(0, int(now - ts))
    ago = (f"{age // 86400}d" if age >= 86400 else f"{age // 3600}h" if age >= 3600
           else f"{age // 60}m" if age >= 60 else f"{age}s")
    return time.strftime("%m-%d %H:%M UTC", time.gmtime(ts)) + f" ({ago} ago)"


def why_reply(user_id: Optional[str], data_dir: str, args: List[str]) -> str:
    if not user_id:
        return "Only the owner can read the refusals."
    n = _DEFAULT_N
    if args:
        try:
            n = int(args[0])
        except ValueError:
            return USAGE
        if n < 1:
            return USAGE
    n = min(n, _MAX_N)
    from core.security_digest import recent_gate_refusals
    try:
        rows = recent_gate_refusals(user_id, data_dir=data_dir, limit=n)
    except Exception as e:
        logger.warning("/why: refusal store unreadable: %s", e)
        return f"Refusals: unavailable({type(e).__name__}: {str(e)[:120]})"
    if not rows:
        return "No refusals recorded."
    now = time.time()
    lines = [f"Last {len(rows)} refusal(s), newest first:"]
    for r in rows:
        detail = " ".join(r["detail"].split())
        if len(detail) > _DETAIL_CHARS:
            detail = detail[:_DETAIL_CHARS - 1] + "…"
        head = f"• {_when(r['ts'], now)} — {r['tool'] or 'no tool'} — {r['reason']}"
        lines.append(head + (f"\n  {detail}" if detail else ""))
    return "\n".join(lines)


__all__ = ["USAGE", "why_reply"]
