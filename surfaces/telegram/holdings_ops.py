"""``/writeoff`` and ``/unquarantine`` — a holding's lifecycle, from the phone (W1).

Contributed verbs (``core.money_verbs`` registers the rows and these handler
references), so the Telegram seat runs them after its owner gate and refuses
them from inside a room. Thin plumbing: the parse, the confirm step and every
sentence live in ``core.wallet.token_trust`` — the REPL
(``cli/ui/commands/h_holdings.py``) and the console call the same functions.

⚠️ REACH, never policy: a write-off moves nothing on-chain. It records the
owner's loss verdict (realized loss = the recorded cost basis) and the tokens
stay in the wallet.
"""
from __future__ import annotations

from typing import Any, List, Optional


def _positions_db(data_dir: Optional[str]) -> Optional[str]:
    if not data_dir:
        return None
    from core.open_positions import open_positions_db_path
    return open_positions_db_path(data_dir)


async def writeoff_verb(*, user_id: str, data_dir: str, args: List[str],
                        task_agent: Any = None, result: Any = None,
                        board: Any = None) -> str:
    from core.wallet.token_trust import writeoff_reply
    return writeoff_reply(user_id, list(args or []),
                          positions_db=_positions_db(data_dir))


async def unquarantine_verb(*, user_id: str, data_dir: str, args: List[str],
                            task_agent: Any = None, result: Any = None,
                            board: Any = None) -> str:
    from core.wallet.token_trust import unquarantine_reply
    return unquarantine_reply(user_id, list(args or []),
                              positions_db=_positions_db(data_dir))
