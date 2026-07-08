"""Owner daily digest — one pushed message written from evidence, not self-report.

A deterministic ($0, no-LLM) composer over the same sources the owner can already
audit — the unified ledger (money in/out), the durable event log (goals moved,
skills changed, sessions run), the goal board's open asks (pending approvals), and
episodes (what ran). Composed in code and pushed via the existing cron delivery
rail (``cron/delivery.deliver_result``), so it costs nothing and cannot
hallucinate. Runs as a cron job carrying ``payload.digest=true`` — the runner
routes such a job here instead of paying for a model turn.

Every read is a module-level seam so tests can monkeypatch it; every read fails
open (a missing source contributes nothing, never an exception).
"""
import time
from typing import Any, Dict, List, Optional


def _ledger(user_id: str, days: int) -> Dict[str, Any]:
    try:
        from core.async_bridge import run_coroutine_sync
        from modules.credits.unified_ledger import build_ledger
        # Loop-safe: compose_digest is awaited inside the cron runner's running
        # loop, where a bare asyncio.run would raise and empty the money section.
        return run_coroutine_sync(build_ledger(user_id, days=max(1, int(days)))) or {}
    except Exception:
        return {}


def _event_aggregate(user_id: str, since_ts: Optional[float]) -> Dict[str, Any]:
    try:
        from agents.task.telemetry.event_log import get_event_log, event_log_enabled
        if not event_log_enabled():
            return {}
        return get_event_log().aggregate(since_ts=since_ts, user_id=user_id) or {}
    except Exception:
        return {}


def _open_asks(user_id: str, data_dir: Optional[str]) -> List[Dict[str, Any]]:
    try:
        import os

        from agents.task.goals.board import GoalBoard
        board = GoalBoard(os.path.join(data_dir or "data", "goals.db"))
        rows = board.asks(user_id=user_id, status="open") or []
        out = []
        for a in rows:
            get = (a.get if isinstance(a, dict) else lambda k, d=None: getattr(a, k, d))
            out.append({"title": get("title") or get("what") or ""})
        return out
    except Exception:
        return []


def _episodes(user_id: str, since_ts: Optional[float]) -> List[Dict[str, Any]]:
    try:
        from core.async_bridge import run_coroutine_sync
        from modules.memory.registry import memory_recall_episodes
        rows = run_coroutine_sync(memory_recall_episodes(
            user_id=user_id, since_ts=int(since_ts) if since_ts else None,
            limit=20, order="newest"))
        out = []
        for r in rows or []:
            get = (r.get if isinstance(r, dict) else lambda k, d=None: getattr(r, k, d))
            out.append({"kind": get("kind"), "outcome": get("outcome")})
        return out
    except Exception:
        return []


async def compose_digest(user_id: str, *, days: int = 1,
                         data_dir: Optional[str] = None, db=None) -> str:
    """Build the digest text from evidence. Pure-ish (all reads fail-open)."""
    since_ts = time.time() - max(1, int(days)) * 86400
    ledger = _ledger(user_id, days)
    agg = _event_aggregate(user_id, since_ts)
    asks = _open_asks(user_id, data_dir)
    episodes = _episodes(user_id, since_ts)

    counts = agg.get("counts_by_kind", {}) or {}
    n_sessions = len(episodes)
    n_goals = int(counts.get("goal_run", 0)) + int(counts.get("goal_completion", 0))
    n_self_mods = int(counts.get("self_modification", 0))
    earned = float(ledger.get("earned_usd") or 0.0)
    spent = float(ledger.get("total_spend_usd") or 0.0)
    net = float(ledger.get("net_usd") or (earned - spent))
    pending_n = int(ledger.get("pending_invoices") or 0)
    pending_usd = float(ledger.get("pending_invoices_usd") or 0.0)

    period = "today" if days == 1 else f"last {days}d"
    lines = [f"Daily digest — {period}:"]
    lines.append(f"• Activity: {n_sessions} session(s), {n_goals} goal event(s), "
                 f"{n_self_mods} self-change(s)")
    lines.append(f"• Money: earned ${earned:.2f}, spent ${spent:.2f}, net ${net:.2f}"
                 + (f"; {pending_n} pending invoice(s) ${pending_usd:.2f}" if pending_n else ""))
    if asks:
        lines.append(f"• Pending approvals ({len(asks)}):")
        for a in asks[:5]:
            title = (a.get("title") or "").strip().replace("\n", " ")[:80]
            lines.append(f"   - {title}")
    else:
        lines.append("• Pending approvals: none")
    return "\n".join(lines)


async def run_digest(task_agent: Any, job: Any) -> bool:
    """Compose the digest for the job's tenant and deliver it out-of-band.

    A $0 tick: no model call. Returns True when a send is attempted-and-succeeds
    (or is intentionally suppressed); False on delivery failure. Never raises."""
    try:
        payload = dict(getattr(job, "payload", {}) or {})
        days = int(payload.get("days", 1) or 1)
        data_dir = payload.get("data_dir")
        text = await compose_digest(getattr(job, "user_id", ""), days=days, data_dir=data_dir)
    except Exception:
        return False
    target = payload.get("deliver") or "telegram"
    try:
        from cron.delivery import deliver_result
        return await deliver_result(task_agent, job, text, target=target,
                                    deliver_target=payload.get("deliver_target"))
    except Exception:
        return False
