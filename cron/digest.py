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
