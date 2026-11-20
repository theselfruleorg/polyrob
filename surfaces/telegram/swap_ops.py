"""``/swap`` on Telegram — the seat half: the words in, the reply out.

The decision (quote, bound, ``go``) is ``tools.defi.owner_verbs.swap`` (proposal
125 S1): the REPL runs the same body, and every tool call meets the seat policy
chain.
"""
from __future__ import annotations

from typing import Any, List, Optional

from tools.defi.owner_verbs.swap import USAGE, WSOL as _WSOL, parse  # noqa: F401
from tools.defi.owner_verbs.swap import swap_reply as _swap_reply


async def swap_reply(user_id: Optional[str], args: List[str], *, tool: Any = None) -> str:
    """``/swap <amount> <in> to <out> on <chain> [slippage <bps>] [min_raw <units>] [max <usd>] [go]``."""
    return await _swap_reply(user_id, args, tool=tool, seat="telegram")


async def swap_verb(*, user_id: str, data_dir: str, args: List[str],
                    task_agent: Any = None, result: Any = None, board: Any = None) -> str:
    """Contributed-verb entry (``core.money_verbs``)."""
    return await swap_reply(user_id, list(args or []))
