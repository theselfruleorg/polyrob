"""Shared owner-facing activity-evidence reads (audit T8, 2026-07-16).

``cron/digest.py`` (daily owner digest) and ``core/recap.py`` (``polyrob recap``)
previously reimplemented the SAME ledger/episode reads; a fix in one silently
missed the other and the owner's money/activity numbers could diverge. Both now
delegate here (their module-level ``_ledger``/``_episodes`` seams stay, as thin
delegates, so existing test monkeypatching keeps working).

Every read fails open (a missing source contributes nothing, never an exception)
and is loop-safe via ``run_coroutine_sync`` — a bare ``asyncio.run`` would raise
inside a running loop (cron runner / REPL dispatch) and silently empty the
section.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def ledger_rollup(user_id: str, days: int, *,
                  include_balances: bool = False) -> Dict[str, Any]:
    """Unified-ledger rollup for the tenant. Fail-open to ``{}``.

    ``{}`` (or an all-zero rollup) means "nothing to show" — callers must not
    render it as an honest-looking $0.00 (H14b).

    include_balances defaults False on purpose: this runs under
    run_coroutine_sync and a balance probe is a network read that would block
    the calling thread (spec §4.1).
    """
    try:
        from core.async_bridge import run_coroutine_sync
        from modules.credits.unified_ledger import build_ledger
        return run_coroutine_sync(build_ledger(
            user_id, days=max(1, int(days)), include_balances=include_balances)) or {}
    except Exception:
        # Sibling fail-opens (_costs_leg, _wallet_leg, _inbound_leg) all warn
        # before swallowing — a silent `return {}` here made a signature drift
        # indistinguishable from "no data yet". Fail-open behavior unchanged;
        # only the observability of a genuine error changes.
        logger.warning("ledger_rollup: unified-ledger read failed (fail-open -> {})", exc_info=True)
        return {}


def recent_episodes(user_id: str, since_ts: Optional[float],
                    limit: int = 20) -> List[Dict[str, Any]]:
    """Newest-first episode rows for the tenant, normalized to plain dicts with
    the superset fields (kind/outcome/spend_usd/task/ts). Fail-open to ``[]``."""
    try:
        from core.async_bridge import run_coroutine_sync
        from modules.memory.registry import memory_recall_episodes
        rows = run_coroutine_sync(memory_recall_episodes(
            user_id=user_id, since_ts=int(since_ts) if since_ts else None,
            limit=limit, order="newest"))
        out: List[Dict[str, Any]] = []
        for r in rows or []:
            # rows may be EpisodeRecord-like or dicts — normalize.
            get = (r.get if isinstance(r, dict) else lambda k, d=None: getattr(r, k, d))
            out.append({"kind": get("kind"), "outcome": get("outcome"),
                        "spend_usd": get("spend_usd", 0.0) or 0.0,
                        "task": get("task") or get("summary") or "",
                        "ts": get("ts")})
        return out
    except Exception:
        return []
