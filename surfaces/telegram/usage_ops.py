"""``/usage [days]`` — what Rob's AI use cost, split (2026-10-14 review D-11 / E-6).

Owner decision D6: one spend view on every seat (Telegram, the console, which
routes through Telegram, and the terminal). The numbers and the lines are
``core.status_economics.spend_view`` / ``spend_view_lines`` — this module only
parses the days and names the scheduled jobs and goals by their task, which
core cannot read. Read-only; an unreadable ledger is said, never shown as $0.
"""
from __future__ import annotations

import time
from typing import List, Optional

USAGE = "Usage: /usage [days] — AI cost by kind of work, job, model and day (default 7 days, up to 90)"
DEFAULT_DAYS = 7


def parse_days(args: List[str]) -> Optional[int]:
    """Days from the verb's words; None for a malformed line."""
    words = [str(a).strip().lower() for a in (args or []) if str(a).strip()]
    if not words:
        return DEFAULT_DAYS
    w = words[0].rstrip("d")
    if len(words) > 1 or not w.isdecimal() or int(w) < 1:
        return None
    return min(int(w), 90)


def usage_reply(user_id: str, data_dir: str, args: List[str], *,
                now: Optional[float] = None) -> str:
    """The ``/usage`` answer on every seat."""
    days = parse_days(args)
    if days is None:
        return USAGE
    from core.status_economics import spend_view, spend_view_lines
    try:
        view = spend_view(user_id, data_dir, now=time.time() if now is None else now,
                          days=days)
    except Exception as e:  # noqa: BLE001 — an unreadable ledger is not $0
        return (f"AI cost: could not read the records ({type(e).__name__}: {str(e)[:160]}) "
                "— this is not a zero.")
    from cron.digest import rail_names
    return "\n".join(spend_view_lines(view, names=rail_names(user_id, data_dir)))


__all__ = ["DEFAULT_DAYS", "USAGE", "parse_days", "usage_reply"]
