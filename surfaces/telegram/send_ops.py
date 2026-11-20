"""``/send`` on Telegram — the seat half: the words in, the reply out.

The decision (quote, bound, ``go``) is ``tools.defi.owner_verbs.send`` (proposal
125 S1): the REPL runs the same body, and every tool call meets the seat policy
chain. Contributed verb (``core.money_verbs``): Telegram runs it after its owner
gate and refuses it in a room.
"""
from __future__ import annotations

from typing import Any, List, Optional

from tools.defi.owner_verbs._seat import owner_ctx
from tools.defi.owner_verbs.send import send_reply as _send_reply
from tools.defi.send_order import (  # noqa: F401  (the one parser, re-exported)
    _EVM_ADDR, _NATIVE, _SOL_ADDR, ANYWAY, USAGE, _checksum_ok, _native_on, parse,
)


def _owner_ctx(user_id: str):
    """The Telegram seat's owner turn for one ``/send``."""
    return owner_ctx(user_id, seat="telegram", verb="send")


async def send_reply(user_id: Optional[str], args: List[str], *, tool: Any = None) -> str:
    """``/send <amount|$usd> <native|token> to <address> on <chain> [go]``."""
    return await _send_reply(user_id, args, tool=tool, seat="telegram")


async def send_verb(*, user_id: str, data_dir: str, args: List[str],
                    task_agent: Any = None, result: Any = None,
                    board: Any = None) -> str:
    """Contributed-verb entry (``core.money_verbs``)."""
    return await send_reply(user_id, list(args or []))
