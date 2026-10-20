"""Owner daily digest — one pushed message written from evidence, not self-report.

A deterministic ($0, no-LLM) composer over the same sources the owner can already
audit — the unified ledger (money in/out), the durable event log (goals moved,
skills changed, sessions run), the goal board's open asks (pending approvals), and
episodes (what ran). Composed in code and pushed via the existing cron delivery
rail (``cron/delivery.deliver_result``), so it costs nothing and cannot
hallucinate. Runs as a cron job carrying ``payload.digest=true`` — the runner
routes such a job here instead of paying for a model turn.

Every read is a module-level seam so tests can monkeypatch it. A seam RAISES
on a failed read and the composer names it on its own line (080 WS-4) — "could
not read" never renders as "none" or "0". An ABSENT source (event log off) is
empty, not a failure.
"""
import logging
import os
import time
from typing import Any, Dict, List, Optional

_logger = logging.getLogger(__name__)


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
    include_balances=True: the digest is a display surface (spec §4.1). Raises."""
    from core.activity_evidence import ledger_rollup_strict
    return ledger_rollup_strict(user_id, days, include_balances=True)


def _event_aggregate(user_id: str, since_ts: Optional[float]) -> Dict[str, Any]:
    """Counts by kind from the durable event log. Raises on a failed read; a
    disabled log is ``{}``."""
    from core.event_log import get_event_log, event_log_enabled
    if not event_log_enabled():
        return {}
    return get_event_log().aggregate(since_ts=since_ts, user_id=user_id) or {}


def _outward(user_id: str, since_ts: float, data_dir: Optional[str]) -> str:
    """033: what the agent did to the outside world, by effect class — every
    tool, surface and MCP server, not a closed kind list. Seam kept for test
    monkeypatching; an unreadable store says so rather than printing "none"."""
    try:
        from core.effects import outward_counts, outward_line
        return outward_line(outward_counts(user_id, since_ts, data_dir=data_dir))
    except Exception:
        return "unavailable (event log unreadable)"


def _open_asks(user_id: str, data_dir: Optional[str]) -> List[Dict[str, Any]]:
    """The open asks on the goal board. Raises on a failed read."""
    from agents.task.goals.board import GoalBoard
    from core.runtime_paths import goals_db_path
    board = GoalBoard(goals_db_path(data_dir))
    rows = board.asks(user_id=user_id, status="open") or []
    out = []
    for a in rows:
        get = (a.get if isinstance(a, dict) else lambda k, d=None: getattr(a, k, d))
        out.append({"title": get("title") or get("what") or ""})
    return out


def _episodes(user_id: str, since_ts: Optional[float]) -> List[Dict[str, Any]]:
    """Seam kept for test monkeypatching; the read lives on the shared layer (T8).
    Rows carry the superset fields (kind/outcome/spend_usd/task/ts) — the digest
    consumes kind/outcome and ignores the rest."""
    from core.activity_evidence import recent_episodes_strict
    return recent_episodes_strict(user_id, since_ts, EPISODE_READ_LIMIT)


def _short(text: Any, n: int = 40) -> str:
    first = " ".join(str(text or "").split())
    return first if len(first) <= n else first[: n - 1] + "…"


def rail_names(user_id: str, data_dir: str) -> Dict[str, str]:
    """``job:<id>`` / ``goal:<id>`` -> a short title. Fail-open to ``{}`` (the
    view then shows the id), because a name is a label, not a number."""
    names: Dict[str, str] = {}
    try:
        from core.runtime_paths import cron_db_path
        from cron.jobs import CronJobStore
        path = cron_db_path(data_dir)
        jobs = CronJobStore(path).list(user_id=user_id) if os.path.exists(path) else []
        for job in jobs or []:
            names[f"job:{job.id}"] = _short(job.task) or f"scheduled job {job.id[:8]}"
    except Exception:
        _logger.debug("names: job names unavailable", exc_info=True)
    try:
        from agents.task.goals.board import GoalBoard
        from core.runtime_paths import goals_db_path
        path = goals_db_path(data_dir)
        goals = (GoalBoard(path).list_recent(user_id=user_id, limit=200)
                 if os.path.exists(path) else [])
        for g in goals or []:
            names[f"goal:{g.id}"] = "goal " + (_short(getattr(g, "title", "")) or g.id[:8])
    except Exception:
        _logger.debug("names: goal names unavailable", exc_info=True)
    return names


def _short(text: Any, n: int = 40) -> str:
    first = " ".join(str(text or "").split())
    return first if len(first) <= n else first[: n - 1] + "…"


def _top_cost(user_id: str, data_dir: Optional[str], days: int) -> str:
    """2026-10-14 D6: ONE digest line — the biggest AI cost of the window
    (``core.status_economics.top_cost_line``). Raises on an unreadable ledger
    (the composer names it)."""
    from core.runtime_paths import data_dir_or_home
    from core.status_economics import spend_view, top_cost_line
    home = data_dir_or_home(data_dir)
    view = spend_view(user_id, home, now=time.time(), days=max(1, int(days)))
    return top_cost_line(view, names=rail_names(user_id, home))


#: The episode read is bounded; a full read renders as "N+" (080 WS-4).
EPISODE_READ_LIMIT = 20


def _read(failures: List[str], source: str, fn, *args):
    """One seam; a failure is recorded by name and the value is ``None``."""
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001 — named on the digest, never a silent blank
        failures.append(f"{source} ({type(e).__name__}: {str(e)[:100]})")
        return None


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
        out = render_health_lines(snap, prefix="• ", limit=6)
        # 080 WS-4: what runs next, by name — the snapshot's ONE Next list.
        nxt = [ln for ln in (snap.sections["loops"].lines or []) if ln.startswith("next: ")]
        if snap.sections["loops"].available and nxt:
            out.append("• Next: " + nxt[0][len("next: "):])
        else:
            out.append(f"• Next: unknown ({snap.sections['loops'].reason or 'no next line'})")
        return out
    except Exception as e:
        return [f"Health: unavailable ({type(e).__name__}: {str(e)[:120]})"]


async def compose_digest(user_id: str, *, days: int = 1,
                         data_dir: Optional[str] = None, db=None) -> str:
    """Build the digest text from evidence. Pure-ish (all reads fail-open)."""
    since_ts = time.time() - max(1, int(days)) * 86400
    failures: List[str] = []
    ledger = _read(failures, "ledger", _ledger, user_id, days)
    agg = _read(failures, "event log", _event_aggregate, user_id, since_ts)
    asks = _read(failures, "open asks", _open_asks, user_id, data_dir)
    episodes = _read(failures, "episodes", _episodes, user_id, since_ts)
    top_cost = _read(failures, "AI cost records", _top_cost, user_id, data_dir, days)

    from core.event_kinds import GOAL_COMPLETION, GOAL_RUN, SELF_MODIFICATION

    def _n(value, unknown: bool) -> str:
        return "?" if unknown else str(value)

    counts = (agg or {}).get("counts_by_kind", {}) or {}
    n_sessions = len(episodes or [])
    sessions = ("?" if episodes is None else
                f"{n_sessions}+" if n_sessions >= EPISODE_READ_LIMIT else str(n_sessions))
    n_goals = int(counts.get(GOAL_RUN, 0)) + int(counts.get(GOAL_COMPLETION, 0))
    n_self_mods = int(counts.get(SELF_MODIFICATION, 0))

    # 080 WS-4: the window is ROLLING, not the calendar day — say so.
    period = "last 24 h" if days == 1 else f"last {days} days"
    lines = [f"Daily digest — {period}:"]
    # 2026-08-28 status SSOT: the digest leads with the same health block
    # /status renders. A degraded instance is the first thing the owner reads;
    # a builder failure is one honest line, never a silently clean digest.
    lines.extend(_health_lines(user_id, data_dir))
    lines.append(f"• Activity: {sessions} session(s), {_n(n_goals, agg is None)} goal event(s), "
                 f"{_n(n_self_mods, agg is None)} self-change(s)")
    if failures:
        lines.append("• Could not read: " + "; ".join(failures)
                     + " — a '?' or a missing line here is NOT a zero")
    lines.append(f"• Outward: {_outward(user_id, since_ts, data_dir)}")

    # H14b (final whole-branch review, Finding 1): `_ledger` fails open to `{}`
    # when the unified-ledger read raises (or the DB isn't there). `{}` and a
    # genuinely quiet day with real zeros used to render the EXACT same
    # "Treasury: income $0.00, spend $0.00, net $0.00" / "Runtime cost: $0.00"
    # lines — the owner had no way to tell "we couldn't read the ledger" from
    # "nothing happened today". This is the same lie shape as the 2026-07-16
    # incident this project exists to fix. Short-circuit to an honest empty
    # state instead of fabricating money lines from an empty dict.
    if ledger is None:
        lines.append("• Money: could not read the ledger (see above)")
    elif not ledger:
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
        except Exception as e:  # noqa: BLE001
            lines.append(f"  ⚠ ledger availability unknown ({type(e).__name__})")

    # 2026-10-14 D6: the one "top cost" line (`/usage` has the full split).
    lines.append(f"• {top_cost}" if top_cost is not None
                 else "• Top AI cost: could not read the records (see above)")

    if asks is None:
        lines.append("• Pending approvals: could not read the open asks (see above)")
    elif asks:
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
