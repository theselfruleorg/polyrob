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


def digest_enabled_for(user_id: Optional[str], home_dir: Optional[str]) -> bool:
    """Owner's digest on/off switch: pref (override, spec ``digest.enabled``)
    over the ``OWNER_DIGEST_ENABLED`` env default. No pref file present =>
    byte-identical to ``AutonomyConfig.owner_digest_enabled()`` (owner-UX P1 T4)."""
    from core.config_policy import AutonomyConfig
    from core import prefs
    env_value = AutonomyConfig.owner_digest_enabled()
    return prefs.resolve("digest.enabled", user_id, home_dir,
                         env_value=env_value, default=env_value)


def _ledger(user_id: str, days: int) -> Dict[str, Any]:
    """Seam kept for test monkeypatching; the read lives on the shared layer (T8).
    include_balances=True: the digest is a display surface (spec §4.1)."""
    from core.activity_evidence import ledger_rollup
    return ledger_rollup(user_id, days, include_balances=True)


def _event_aggregate(user_id: str, since_ts: Optional[float]) -> Dict[str, Any]:
    try:
        from core.event_log import get_event_log, event_log_enabled
        if not event_log_enabled():
            return {}
        return get_event_log().aggregate(since_ts=since_ts, user_id=user_id) or {}
    except Exception:
        return {}


def _open_asks(user_id: str, data_dir: Optional[str]) -> List[Dict[str, Any]]:
    try:
        import os

        from agents.task.goals.board import GoalBoard
        from core.runtime_paths import goals_db_path
        board = GoalBoard(goals_db_path(data_dir))
        rows = board.asks(user_id=user_id, status="open") or []
        out = []
        for a in rows:
            get = (a.get if isinstance(a, dict) else lambda k, d=None: getattr(a, k, d))
            out.append({"title": get("title") or get("what") or ""})
        return out
    except Exception:
        return []


def _episodes(user_id: str, since_ts: Optional[float]) -> List[Dict[str, Any]]:
    """Seam kept for test monkeypatching; the read lives on the shared layer (T8).
    Rows carry the superset fields (kind/outcome/spend_usd/task/ts) — the digest
    consumes kind/outcome and ignores the rest."""
    from core.activity_evidence import recent_episodes
    return recent_episodes(user_id, since_ts)


#: How many suppressed owner messages the digest quotes in full; the rest are counted.
MISSED_MAX_LINES = 8
#: How many rows to read back — bounded, the window filter runs on top.
MISSED_READ_LIMIT = 40


def _missed(user_id: str, data_dir: Optional[str], n: int) -> List[Dict[str, Any]]:
    """Seam: the cap-/pause-suppressed owner notices (`core/surfaces/missed.py`).
    Raises on an unreadable store — the caller renders the reason."""
    from core.surfaces.missed import missed_notices
    from core.runtime_paths import effective_data_home
    return missed_notices(user_id, data_dir or str(effective_data_home()), n=n)


def _missed_lines(user_id: str, data_dir: Optional[str], since_ts: float) -> List[str]:
    """The 'Missed owner messages' section. `user_delivery.py` suppresses a
    capped/paused owner message ON THE PROMISE that it is rolled into the
    digest; until 2026-09-20 no digest path read them back (195/196 suppressed
    over 8 days never reached the owner). Window-bounded, oldest first,
    kind-tagged, capped at MISSED_MAX_LINES with the remainder counted. An
    unreadable store is NAMED — 'none' must never mean 'could not tell'."""
    try:
        rows = _missed(user_id, data_dir, MISSED_READ_LIMIT)
    except Exception as e:  # noqa: BLE001 — render the reason, never a silent blank
        return [f"• Missed owner messages: unreadable ({type(e).__name__}: {str(e)[:120]})"]
    inwin = [r for r in rows if float(r.get("ts") or 0) >= since_ts]
    if not inwin:
        return ["• Missed owner messages: none"]
    inwin.sort(key=lambda r: float(r.get("ts") or 0))
    out = [f"• Missed owner messages ({len(inwin)}) — held by the send cap or a pause, "
           f"rolled up here:"]
    for r in inwin[:MISSED_MAX_LINES]:
        stamp = time.strftime("%m-%d %H:%M", time.gmtime(float(r.get("ts") or 0)))
        text = " ".join(str(r.get("text") or "").split())[:160]
        out.append(f"   - {stamp}Z [{r.get('kind') or 'undelivered'}] {text}")
    if len(inwin) > MISSED_MAX_LINES:
        out.append(f"   … and {len(inwin) - MISSED_MAX_LINES} more (`/missed` shows them)")
    return out


def _health_lines(user_id: str, data_dir: Optional[str]) -> List[str]:
    """Seam kept module-level for test monkeypatching (like ``_ledger``)."""
    try:
        from core.status_snapshot import build_status_snapshot
        from core.status_render import render_health_lines
        snap = build_status_snapshot(user_id, data_dir=data_dir, include_money=False)
        return render_health_lines(snap, prefix="• ", limit=6)
    except Exception as e:
        return [f"Health: unavailable ({type(e).__name__}: {str(e)[:120]})"]


async def compose_digest(user_id: str, *, days: int = 1,
                         data_dir: Optional[str] = None, db=None) -> str:
    """Build the digest text from evidence. Pure-ish (all reads fail-open)."""
    since_ts = time.time() - max(1, int(days)) * 86400
    ledger = _ledger(user_id, days)
    agg = _event_aggregate(user_id, since_ts)
    asks = _open_asks(user_id, data_dir)
    episodes = _episodes(user_id, since_ts)

    from core.event_kinds import GOAL_COMPLETION, GOAL_RUN, SELF_MODIFICATION
    counts = agg.get("counts_by_kind", {}) or {}
    n_sessions = len(episodes)
    n_goals = int(counts.get(GOAL_RUN, 0)) + int(counts.get(GOAL_COMPLETION, 0))
    n_self_mods = int(counts.get(SELF_MODIFICATION, 0))

    period = "today" if days == 1 else f"last {days}d"
    lines = [f"Daily digest — {period}:"]
    # 2026-08-28 status SSOT: the digest leads with the same health block
    # /status renders. A degraded instance is the first thing the owner reads;
    # a builder failure is one honest line, never a silently clean digest.
    lines.extend(_health_lines(user_id, data_dir))
    lines.append(f"• Activity: {n_sessions} session(s), {n_goals} goal event(s), "
                 f"{n_self_mods} self-change(s)")

    # H14b (final whole-branch review, Finding 1): `_ledger` fails open to `{}`
    # when the unified-ledger read raises (or the DB isn't there). `{}` and a
    # genuinely quiet day with real zeros used to render the EXACT same
    # "Treasury: income $0.00, spend $0.00, net $0.00" / "Runtime cost: $0.00"
    # lines — the owner had no way to tell "we couldn't read the ledger" from
    # "nothing happened today". This is the same lie shape as the 2026-07-16
    # incident this project exists to fix. Short-circuit to an honest empty
    # state instead of fabricating money lines from an empty dict.
    if not ledger:
        lines.append("• Money: no data (ledger unreadable)")
    else:
        t = ledger.get("treasury") or {}
        r = ledger.get("runtime") or {}
        income = float(t.get("income_usd") or 0.0)
        t_spend = float(t.get("spend_usd") or 0.0)
        t_net = float(t.get("net_usd") or 0.0)
        t_balance = t.get("balance_usd")
        pending_n = int(t.get("pending_count") or 0)
        pending_usd = float(t.get("pending_usd") or 0.0)
        r_window = float(r.get("spend_window_usd") or 0.0)
        r_total = float(r.get("spend_total_usd") or 0.0)
        r_balance = r.get("provider_balance_usd")

        treasury_line = f"• Treasury: income ${income:.2f}, spend ${t_spend:.2f}, net ${t_net:.2f}"
        if t_balance is not None:
            treasury_line += f" · balance ${t_balance:.2f}"
        if pending_n:
            treasury_line += f"; {pending_n} pending invoice(s) ${pending_usd:.2f}"
        lines.append(treasury_line)
        runtime_line = f"• Runtime cost: ${r_window:.2f} {period} · ${r_total:.2f} total"
        if r_balance is not None:
            runtime_line += f" · balance ${r_balance:.2f}"
        lines.append(runtime_line)

        # Partial degradation (SOME legs read, some didn't — distinct from the
        # fully-unreadable `{}` case above) still renders the numbers it has,
        # annotated so the owner knows a leg is absent rather than zero.
        # Mirrors core/recap.py and cli/ui/commands/h_finance.py.
        try:
            from modules.credits.unified_ledger import ledger_availability_note
            note = ledger_availability_note(ledger)
            if note:
                lines.append(f"  ⚠ {note}")
        except Exception:
            pass

    if asks:
        lines.append(f"• Pending approvals ({len(asks)}):")
        for a in asks[:5]:
            title = (a.get("title") or "").strip().replace("\n", " ")[:80]
            lines.append(f"   - {title}")
    else:
        lines.append("• Pending approvals: none")
    lines.extend(_missed_lines(user_id, data_dir, since_ts))
    # QW-3 (proposal 021): hand the owner the console — WEBVIEW_PUBLIC_URL
    # unset (no public console) keeps the digest byte-identical.
    try:
        from core.surfaces.deep_link import webview_public_url
        base = webview_public_url()
        if base:
            lines.append(f"• Console: {base}")
    except Exception:
        pass
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
    # owner-UX P1 T4: an explicit payload.deliver still wins (a seeded job's own
    # setting); absent that, the digest.channel pref overrides the "telegram"
    # legacy default. Reuses the SAME data_dir already resolved above (no new
    # global default) — falls back to "data" (this function's existing
    # convention, mirroring _open_asks) when the job set none.
    try:
        from cron.delivery import effective_digest_channel
        from core.runtime_paths import data_dir_or_home
        target = payload.get("deliver") or effective_digest_channel(
            getattr(job, "user_id", ""), data_dir_or_home(data_dir))
    except Exception:
        target = payload.get("deliver") or "telegram"
    try:
        from cron.delivery import deliver_result
        return await deliver_result(task_agent, job, text, target=target,
                                    deliver_target=payload.get("deliver_target"))
    except Exception:
        return False
