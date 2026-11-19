"""``/usage [days]`` — what the agent's AI use cost, split (2026-10-14 review D-11 / E-6).

Owner decision D6: one spend view on every seat (Telegram, the console, which
routes through Telegram, and the terminal) and, since 120 W3, the agent's own
``usage_summary``. The parser, the numbers and the text are
``core.status_economics.parse_days`` / ``spend_reply`` — this module only names
the scheduled jobs and goals by their task, which core cannot read. Read-only; an
unreadable ledger is said, never shown as $0.
"""
from __future__ import annotations

from typing import List, Optional

USAGE = "Usage: /usage [days] — AI cost by kind of work, job, model and day (default 7 days, up to 90)"


def usage_reply(user_id: str, data_dir: str, args: List[str], *,
                now: Optional[float] = None) -> str:
    """The ``/usage`` answer on every seat."""
    from core.status_economics import parse_days, spend_reply
    days = parse_days(args)
    if days is None:
        return USAGE

    def _names():
        from cron.digest import rail_names
        return rail_names(user_id, data_dir)

    return spend_reply(user_id, data_dir, days, names=_names, now=now)


__all__ = ["USAGE", "usage_reply"]
