"""`/journey` — a narrative timeline of what the agent did, learned, earned, changed.

A consumer over data POLYROB already produces — episodes (``recall_episodes``),
the durable event log (``self_modification`` / ``payment_settled`` / goal events),
authored-skill provenance (``skill_usage``), and the unified ledger. NOT new
machinery. Every source is read through a module-level seam function so the REPL
handler and the ``polyrob journey`` Click command share one pure renderer, and
tests can monkeypatch each source independently. Every read fails open to an
empty section — a missing provider / disabled flag never breaks the timeline.

No ``from __future__ import annotations`` (kept consistent with the CLI command
modules; unnecessary here).
"""
import time
from typing import Any, Dict, List, Optional


def _window_seconds(label: str) -> Optional[float]:
    """Parse '30m'/'24h'/'7d' -> seconds; None if unset/bad (=> all time)."""
    if not label:
        return None
    label = label.strip().lower()
    try:
        if label.endswith("m"):
            return float(label[:-1]) * 60
        if label.endswith("h"):
            return float(label[:-1]) * 3600
        if label.endswith("d"):
            return float(label[:-1]) * 86400
        return float(label)
    except Exception:
        return None


def _episodes(user_id: str, since_ts: Optional[float]) -> List[Dict[str, Any]]:
    """Episodes for the tenant (what I did) via the memory registry. Fail-open."""
    try:
        from core.async_bridge import run_coroutine_sync
        from modules.memory.registry import memory_recall_episodes
        # run_coroutine_sync is loop-safe: bare asyncio.run would raise inside the
        # REPL's running event loop (dispatch awaits this sync handler), silently
        # emptying this section.
        rows = run_coroutine_sync(memory_recall_episodes(
            user_id=user_id, since_ts=int(since_ts) if since_ts else None,
            limit=20, order="newest"))
        out = []
        for r in rows or []:
            # rows may be EpisodeRecord-like or dicts — normalize.
            get = (r.get if isinstance(r, dict) else lambda k, d=None: getattr(r, k, d))
            out.append({"kind": get("kind"), "outcome": get("outcome"),
                        "spend_usd": get("spend_usd", 0.0) or 0.0,
                        "task": get("task") or get("summary") or ""})
        return out
    except Exception:
        return []


def _events(user_id: str, since_ts: Optional[float]) -> List[Dict[str, Any]]:
    """Durable event-log rows for the tenant (changed/earned). Fail-open."""
    try:
        from agents.task.telemetry.event_log import get_event_log, event_log_enabled
        if not event_log_enabled():
            return []
        return get_event_log().query(
            user_id=user_id, since_ts=since_ts, limit=200) or []
    except Exception:
        return []


def _authored(user_id: str, data_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """Authored skills + reuse counts for the tenant (learned). Fail-open."""
    try:
        from modules.skills.skill_usage import get_skill_usage_store
        return get_skill_usage_store(data_dir).list_authored(user_id=user_id) or []
    except Exception:
        return []


def _ledger(user_id: str, days: int) -> Dict[str, Any]:
    """Unified ledger rollup for the tenant (earned/spent). Fail-open to zeros."""
    try:
        from core.async_bridge import run_coroutine_sync
        from modules.credits.unified_ledger import build_ledger
        return run_coroutine_sync(build_ledger(user_id, days=max(1, int(days)))) or {}
    except Exception:
        return {}


def render_journey(*, user_id: str, since_label: str = "7d",
                   data_dir: Optional[str] = None) -> str:
    """Pure renderer: union the four sources into a plain-text timeline."""
    secs = _window_seconds(since_label)
    since_ts = (time.time() - secs) if secs else None
    days = max(1, int((secs or 7 * 86400) // 86400))
    scope = f"last {since_label}" if secs else "all time"

    episodes = _episodes(user_id, since_ts)
    events = _events(user_id, since_ts)
    authored = _authored(user_id, data_dir)
    ledger = _ledger(user_id, days)

    lines: List[str] = [f"journey — {scope}"]

    # Did — episodes
    lines.append("")
    lines.append("Did:")
    if episodes:
        for e in episodes[:20]:
            spend = f" ${float(e.get('spend_usd') or 0):.2f}" if e.get("spend_usd") else ""
            task = (e.get("task") or "").strip().replace("\n", " ")[:80]
            lines.append(f"  - {e.get('kind') or '?'}:{e.get('outcome') or '?'}{spend} \"{task}\"")
    else:
        lines.append("  (no episodes recorded)")

    # Earned — ledger + payment_settled events
    earned = float(ledger.get("earned_usd") or 0.0)
    spent = float(ledger.get("total_spend_usd") or 0.0)
    net = float(ledger.get("net_usd") or (earned - spent))
    settled = int(ledger.get("settled_payments") or 0)
    lines.append("")
    lines.append(
        f"Earned: ${earned:.2f} ({settled} settled) · spent ${spent:.2f} · net ${net:.2f}")

    # Learned — authored skills
    lines.append("")
    lines.append("Learned:")
    if authored:
        for s in authored[:20]:
            sid = s.get("skill_id") or "?"
            loads = s.get("load_count") if s.get("load_count") is not None else s.get("loads", 0)
            by = s.get("created_by") or ""
            by = f" [{by}]" if by else ""
            lines.append(f"  - {sid}{by} (used {loads}x)")
    else:
        lines.append("  (no authored skills)")

    # Changed — self_modification events
    changes = [e for e in events if e.get("kind") == "self_modification"]
    lines.append("")
    lines.append("Changed:")
    if changes:
        for c in changes[:20]:
            a = c.get("attrs") or {}
            what = a.get("kind") or a.get("action") or "self_modification"
            ident = a.get("skill_id") or a.get("id") or ""
            ident = f" {ident}" if ident else ""
            lines.append(f"  - {what}{ident}")
    else:
        lines.append("  (no self-modifications)")

    return "\n".join(lines)


def h_journey(ctx) -> None:
    """REPL handler: /journey [window]  e.g. /journey 24h, /journey 7d."""
    since_label = ctx.args[0] if getattr(ctx, "args", None) else "7d"
    uid = (getattr(ctx, "user_id", "") or "").strip() or "local"
    data_dir = None
    container = getattr(ctx, "container", None)
    if container is not None:
        cfg = getattr(container, "config", None)
        data_dir = getattr(cfg, "data_dir", None)
    text = render_journey(user_id=uid, since_label=since_label, data_dir=data_dir)
    ctx.emit(text, title="journey")
