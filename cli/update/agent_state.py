"""Perceive the agent's state before an update, and wait for the wrap-up (058).

``polyrob update --apply`` used to refuse the moment anything looked busy and
tell the operator to stop the agent. A code swap under a running turn, a goal
run or a cron rail is the harm; a turn that ends in forty seconds is not a
reason to give up. The busy signals are read by ONE reader,
``core.agent_activity`` (proposal 125 S13) — the same one the 077 drain and
``scripts/deploy_when_idle.sh`` call. This module re-exports it and adds the
bounded wait.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

from core.agent_activity import (  # noqa: F401  (re-exported for the CLI)
    DEFAULT_CRON_IMMINENT_SEC,
    AgentActivity,
    observe,
)

DEFAULT_WAIT_SEC = 1800
DEFAULT_POLL_SEC = 15


def wait_for_wrap_up(data_home: Path, *, timeout: float = DEFAULT_WAIT_SEC,
                     poll: float = DEFAULT_POLL_SEC,
                     on_tick: Optional[Callable[[AgentActivity, float], None]] = None,
                     cron_imminent_sec: int = DEFAULT_CRON_IMMINENT_SEC) -> AgentActivity:
    """Poll ``observe`` until idle or *timeout*; the result carries ``waited``.
    ``on_tick(activity, waited)`` is called on every busy observation so the
    operator sees WHAT the update is waiting on, not a silent pause."""
    start = time.monotonic()
    while True:
        act = observe(data_home, cron_imminent_sec=cron_imminent_sec)
        act.waited = time.monotonic() - start
        if act.idle or act.waited >= timeout:
            return act
        if on_tick is not None:
            on_tick(act, act.waited)
        time.sleep(poll)
