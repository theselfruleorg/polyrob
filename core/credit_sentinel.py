"""§6.3 provider-credit sentinel — no more silent multi-day 402 grind.

Live evidence (2026-07-07): 465× OpenRouter 402 in one day, zero owner-facing
signal, autonomy effectively dead for two days while cron/goal tickers kept
grinding paid-looking runs. The sentinel turns a credit-death refusal from an
autonomous run into:

1. ONE safety-net notice through the §3.2 delivery rail (rail dedup + the
   already-active check make it once per episode);
2. a durable file latch (``<data_root>/CREDIT_SENTINEL``, JSON with the trip
   timestamp) that pauses goal dispatch and LLM cron ticks — $0 ticks
   (digest, wake_agent=false) keep flowing;
3. AUTO-RELEASE after ``CREDIT_SENTINEL_RELEASE_HOURS`` — one paid probe per
   window instead of a permanent manual halt (`rm` the file or set the env
   flag off to release early).

Modeled on the ``AUTONOMY_HALT`` latch (constants.autonomy_halted) but
time-bounded and self-releasing. Everything here is fail-open.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

from core.env import bool_env, int_env

logger = logging.getLogger(__name__)

SENTINEL_FILENAME = "CREDIT_SENTINEL"

_CREDIT_DEATH_MARKERS = (
    "402",
    "insufficient_quota",
    "insufficient credits",
    "payment required",
    "billing",
    "credit balance",
)


def credit_sentinel_enabled() -> bool:
    return bool_env("CREDIT_SENTINEL_ENABLED", True)


def _release_hours() -> int:
    return int_env("CREDIT_SENTINEL_RELEASE_HOURS", 6)


def _sentinel_path() -> str:
    try:
        from core.runtime_config import get_data_root
        base = get_data_root()
    except Exception:
        # Fallback mirrors the SSOT precedence (resolve_data_home: POLYROB_DATA_DIR
        # → cwd/.polyrob), NOT the legacy "POLYROB_DATA_DIR or DATA_ROOT or 'data'"
        # order — DATA_ROOT is the SESSION-tree axis, and a sentinel latched there
        # is a safety gate reading a different file than the agent writes
        # (structural audit T3, 2026-07-16).
        try:
            from core.runtime_paths import resolve_data_home
            base = str(resolve_data_home())
        except Exception:
            base = os.getenv("POLYROB_DATA_DIR") or "data"
    return os.path.join(base, SENTINEL_FILENAME)


def looks_like_credit_death(text: Optional[str]) -> bool:
    """Does a run-refusal status / exception string look like provider credit
    death? Called on framework status strings ("Session failed: PERMANENT
    ERROR: … 402 …"), not on agent prose."""
    if not text:
        return False
    low = str(text).lower()
    return any(m in low for m in _CREDIT_DEATH_MARKERS)


def credit_sentinel_active() -> bool:
    """True while the latch is fresh; an expired latch auto-releases (removed)."""
    if not credit_sentinel_enabled():
        return False
    path = _sentinel_path()
    try:
        if not os.path.exists(path):
            return False
        try:
            with open(path) as f:
                ts = float((json.load(f) or {}).get("ts") or 0.0)
        except Exception:
            # Unreadable latch: treat its mtime as the trip time.
            ts = os.path.getmtime(path)
        if time.time() - ts >= _release_hours() * 3600:
            try:
                os.remove(path)
                logger.info("credit sentinel auto-released after %sh", _release_hours())
            except OSError:
                pass
            return False
        return True
    except Exception:
        return False  # fail-open: a broken latch never blocks autonomy


async def trip_credit_sentinel(reason: str, *, container: Any = None,
                               user_id: str = "") -> bool:
    """Activate the latch + send the one §3.4 safety-net notice. Idempotent
    while active; never raises."""
    if not credit_sentinel_enabled():
        return False
    already = credit_sentinel_active()
    if not already:
        try:
            path = _sentinel_path()
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w") as f:
                json.dump({"ts": time.time(), "reason": str(reason)[:500]}, f)
        except Exception:
            logger.warning("credit sentinel: latch write failed", exc_info=True)
        try:
            from agents.task.telemetry.event_log import get_event_log
            get_event_log().record("credit_sentinel", user_id=str(user_id or ""),
                                   source="credit_sentinel",
                                   attrs={"reason": str(reason)[:500]})
        except Exception:
            pass
        try:
            import core.surfaces.user_delivery as _ud
            text = (f"⛔ Autonomy paused: provider credit failure — {str(reason)[:300]}. "
                    f"Goal dispatch and LLM cron ticks resume automatically in "
                    f"{_release_hours()}h (or remove {_sentinel_path()} after topping up).")
            await _ud.deliver_user_message(container, str(user_id or ""), text,
                                           source="credit_sentinel")
        except Exception:
            logger.debug("credit sentinel: notice failed (durable fallback already "
                         "handled by the rail)", exc_info=True)
    return True
