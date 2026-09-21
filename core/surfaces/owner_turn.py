"""ONE way for a headless surface to say "a human is mid-turn".

`core.interactive_gate.owner_turn` is the primitive: it marks the process busy,
writes `<data>/locks/turn.active`, and takes the cross-process workspace lock
without ever refusing the human. Until 057 WS-H only `surfaces/telegram/
harness.py` opened it, so a turn driven from any other seat was invisible to
the goal/cron ticks and to `scripts/deploy_when_idle.sh` — which is how a
deploy landed at step 8 of a live owner turn (and at step 4 of another at
10:36Z on 2026-09-19).

This wrapper exists so the other seats do not each re-derive the call: it is
fail-open (a marker fault must never cost the human their turn — the caller
gets ``False`` and the turn proceeds unmarked) and it is revertible with one
flag.

Note the email surface is ALREADY covered: it executes through the shared
`surfaces.telegram.harness.act_on_inbound` → `_run_and_deliver`, which opens
`owner_turn` itself. `tests/unit/surfaces/test_owner_turn_helper.py` pins that
so the coverage cannot be lost by a refactor of the shared executor.
"""
from __future__ import annotations

import contextlib
import logging
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

#: Revert switch for the whole helper. ON = a console/API-driven human turn
#: holds the workspace and blocks a deploy for its duration, as a Telegram turn
#: already does. OFF = the pre-057 behaviour, byte-identical.
FLAG = "SURFACE_OWNER_TURN"


def surface_owner_turn_enabled() -> bool:
    # The literal is spelled here on purpose: `tests/unit/core/test_flags_reverse.py`
    # scans for `bool_env(<literal name>, …)`, and a flag read through a constant is a flag
    # the catalog can never learn about.
    from core.env import bool_env
    return bool_env("SURFACE_OWNER_TURN", True)


@contextlib.contextmanager
def surface_owner_turn(kind: str = "owner_chat", session_id: Optional[str] = None,
                       lock_timeout: float = 5.0) -> Iterator[bool]:
    """Hold the human-turn gate for the body. Yields whether it was held.

    Never raises for a gate fault, and never refuses the human: an unwritable
    marker, an absent data home or a contended lock all yield ``False`` and the
    turn runs exactly as it did before.
    """
    if not surface_owner_turn_enabled():
        yield False
        return
    with contextlib.ExitStack() as stack:
        held = False
        try:
            from core.interactive_gate import owner_turn
            stack.enter_context(owner_turn(kind=kind, session_id=session_id,
                                           lock_timeout=lock_timeout))
            held = True
        except Exception:  # a marker fault must never cost the human their turn
            logger.debug("surface owner-turn gate not held (kind=%s session=%s)",
                         kind, session_id, exc_info=True)
        yield held
