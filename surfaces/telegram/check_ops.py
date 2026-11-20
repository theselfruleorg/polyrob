"""``/check`` on Telegram — the seat half: the words in, the reply out.

The read is the agent's own ``defi_data.address_report`` (proposal 125 S12),
run by ``tools.defi.owner_verbs.check`` through the seat policy chain; the REPL
runs the same body.
"""
from __future__ import annotations

from typing import List, Optional

from tools.defi.owner_verbs.check import USAGE  # noqa: F401
from tools.defi.owner_verbs.check import check_reply as _check_reply


async def check_reply(user_id: Optional[str], args: List[str]) -> str:
    return await _check_reply(user_id, args, seat="telegram")


async def check_verb(*, user_id: str, data_dir: str, args: List[str],
                     task_agent=None, result=None, board=None) -> str:
    """Contributed-verb entry (``core.money_verbs``)."""
    return await check_reply(user_id, list(args or []))
