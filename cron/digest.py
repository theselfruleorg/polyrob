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
    """The ONE switch lives in ``core.owner_digest`` (status reads it too)."""
    from core.owner_digest import digest_enabled_for as _enabled
    return _enabled(user_id, home_dir)


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


def _proposals(user_id: str, since_ts: float, data_dir: Optional[str]) -> List[str]:
    """The curator's owner proposal lines (095 W12) — the recap's one reader."""
    from core.recap import curator_proposals
    return curator_proposals(user_id, since_ts, data_dir)


def _mechanism_notes(user_id: str, since_ts: float, data_dir: Optional[str]) -> List[str]:
    """The reviewers' notes for the developer (108 W6): a fault no skill can fix."""
    from core.recap import mechanism_notes
    return mechanism_notes(user_id, since_ts, data_dir)


def _outward(user_id: str, since_ts: float, data_dir: Optional[str]) -> str:
    """033: what the agent did to the outside world, by effect class — every
    tool, surface and MCP server, not a closed kind list. Seam kept for test
    monkeypatching; an unreadable store says so rather than printing "none"."""
    try:
        from core.effects import outward_counts, outward_line
        return outward_line(outward_counts(user_id, since_ts, data_dir=data_dir))
    except Exception:
        return "unavailable (event log unreadable)"


def _snapshot(user_id: str, data_dir: Optional[str], days: int = 1):
    """The ONE status snapshot the digest renders from (116 W7.4): the health
    block, the open asks and the missed messages are its sections, over the
    digest's own window. Raises; the composer names the failure."""
    from core.status_snapshot import build_status_snapshot
    return build_status_snapshot(user_id, data_dir=data_dir, include_money=False,
                                 window_hours=24 * max(1, int(days)))


class _OneSnapshot:
    """The digest's snapshot, built on first use and at most once (a failed build
    is kept and re-raised, so every reader names the same failure)."""

    def __init__(self, user_id: str, data_dir: Optional[str], days: int):
        self._args, self._value, self._error = (user_id, data_dir, days), None, None

    def get(self):
        if self._value is None and self._error is None:
            try:
                self._value = _snapshot(*self._args)
            except Exception as e:  # noqa: BLE001 — re-raised to every reader below
                self._error = e
        if self._error is not None:
            raise self._error
        return self._value


def _snap(snap, user_id: str, data_dir: Optional[str]):
    """*snap* as a snapshot: a :class:`_OneSnapshot`, a built one, or None (build)."""
    if isinstance(snap, _OneSnapshot):
        return snap.get()
    return snap if snap is not None else _snapshot(user_id, data_dir)


def _section(snap, name: str):
    """A snapshot section that could be read, else RAISE with its reason (an
    unreadable section is never rendered as an empty one)."""
    sec = snap.sections.get(name)
    if sec is None or not sec.available:
        raise RuntimeError(f"{name} section unavailable ({getattr(sec, 'reason', '') or '?'})")
    return sec


def _open_asks(user_id: str, data_dir: Optional[str], snap=None) -> List[Dict[str, Any]]:
    """The owner's open asks: ``work.data["open_asks"]`` — the selection /status
    counts (an ask decided on /pending is in :func:`_pending_items`). Raises."""
    snap = _snap(snap, user_id, data_dir)
    return [{"id": a.get("id") or "", "title": a.get("title") or "",
             "created_at": a.get("created_at")}
            for a in _section(snap, "work").data.get("open_asks") or []]


def _pending_items(user_id: str, data_dir: Optional[str], snap=None) -> List[Dict[str, Any]]:
    """What waits on /pending: ``approvals.data["items"]`` (tool approvals, drafts,
    contacts). Raises."""
    snap = _snap(snap, user_id, data_dir)
    return list(_section(snap, "approvals").data.get("items") or [])


def _failed_jobs(user_id: str, data_dir: Optional[str]) -> List[Any]:
    """Owner-armed jobs whose LAST run failed or ended partial (routines review
    2026-10-29: the 10-28 announce failure was not on the 10-29 digest's visible
    lines, below five housekeeping items), and any job the failure limit paused.
    Raises on an unreadable store."""
    from core.cron_outcome import FAILED_OUTCOMES, agent_armed, stopped_by_failures
    from core.runtime_paths import cron_db_path, data_dir_or_home
    from cron.jobs import CronJobStore
    path = cron_db_path(data_dir_or_home(data_dir))
    if not os.path.exists(path):
        return []
    out = []
    for job in CronJobStore(path).list(user_id=user_id) or []:
        p = job.payload or {}
        if job.status == "cancelled" or p.get("digest"):
            continue
        if agent_armed(p) and not stopped_by_failures(p):
            continue
        if (p.get("last_outcome") or {}).get("outcome") in FAILED_OUTCOMES:
            out.append(job)
    return out


def _failed_job_lines(jobs: Optional[List[Any]], since_ts: float) -> List[str]:
    """The FIRST section after the header: a job of the owner's that failed in
    the window outranks every housekeeping line below it."""
    if not jobs:
        return []
    from datetime import datetime
    from cron.service import last_outcome, next_run_text
    recent = []
    for job in jobs:
        at = str(((job.payload or {}).get("last_outcome") or {}).get("at") or "")
        try:
            ts = datetime.fromisoformat(at).timestamp()
        except ValueError:
            ts = None  # a failure with no readable time is still shown
        if ts is None or ts >= since_ts:
            recent.append(job)
    if not recent:
        return []
    out = [f"• ⚠ Your jobs that failed ({len(recent)}) — their work did not finish:"]
    for job in recent[:5]:
        p = job.payload or {}
        out.append(f"   - {job.id[:8]} \"{_short(p.get('name') or job.task, 50)}\" "
                   f"{last_outcome(job)} · deliver: {p.get('deliver') or 'none'} · "
                   f"next {next_run_text(job)} — /job show {job.id[:8]}")
    if len(recent) > 5:
        out.append(f"   … and {len(recent) - 5} more (/jobs)")
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


#: How many curator proposal lines the digest quotes.
PROPOSALS_MAX_LINES = 5
#: How many suppressed owner messages the digest quotes in full; the rest are
#: counted. The snapshot holds at most ``core.status_types.MISSED_IN_SNAPSHOT``.
MISSED_MAX_LINES = 8


def _missed(user_id: str, data_dir: Optional[str], snap=None) -> tuple:
    """Seam: ``(entries, left_out, window label)`` — the snapshot's delivery
    section (``core/surfaces/missed.py``: the ``/missed`` selection and its
    ``missed_count``, since the owner's last ``/missed`` else the window).
    Raises on an unreadable store — the caller renders the reason."""
    snap = _snap(snap, user_id, data_dir)
    d = _section(snap, "delivery").data
    if d.get("missed_unavailable"):
        raise RuntimeError(str(d["missed_unavailable"]))
    return list(d.get("missed") or []), int(d.get("missed_more") or 0), d.get("missed_window") or ""


def _missed_lines(user_id: str, data_dir: Optional[str], snap=None) -> List[str]:
    """The 'Missed owner messages' section. `user_delivery.py` suppresses a
    capped/paused owner message ON THE PROMISE that it is rolled into the
    digest; until 2026-09-20 no digest path read them back (195/196 suppressed
    over 8 days never reached the owner). The count and window are /status's
    own (116 W7.4); oldest first, kind-tagged, capped at MISSED_MAX_LINES with
    the remainder counted. An unreadable store is NAMED — 'none' must never
    mean 'could not tell'."""
    try:
        rows, more, label = _missed(user_id, data_dir, snap)
    except Exception as e:  # noqa: BLE001 — render the reason, never a silent blank
        return [f"• Missed owner messages: unreadable ({type(e).__name__}: {str(e)[:120]})"]
    if not rows:
        return ["• Missed owner messages: none" + (f" {label}" if label else "")]
    rows = sorted(rows, key=lambda r: float(r.get("ts") or 0))
    shown, total = rows[:MISSED_MAX_LINES], len(rows) + more
    out = [f"• Missed owner messages ({total}{' ' + label if label else ''}) — held by the "
           f"send cap or a pause, rolled up here:"]
    for r in shown:
        stamp = time.strftime("%m-%d %H:%M", time.gmtime(float(r.get("ts") or 0)))
        text = " ".join(str(r.get("text") or "").split())[:160]
        out.append(f"   - {stamp}Z [{r.get('kind') or 'undelivered'}] {text}")
    if total > len(shown):
        out.append(f"   … and {total - len(shown)} more (`/missed` shows them)")
    return out


def _health_lines(user_id: str, data_dir: Optional[str], snap=None) -> List[str]:
    """Seam kept module-level for test monkeypatching (like ``_ledger``)."""
    try:
        from core.status_render import render_health_lines
        snap = _snap(snap, user_id, data_dir)
        # The same number of health items as a chat /status (one constant).
        from core.status_render import SEAT_HEALTH_LIMIT
        out = render_health_lines(snap, prefix="• ", limit=SEAT_HEALTH_LIMIT)
        # 080 WS-4: what runs next, by name — the snapshot's ONE Next list.
        nxt = [ln for ln in (snap.sections["loops"].lines or []) if ln.startswith("next: ")]
        if snap.sections["loops"].available and nxt:
            out.append("• Next: " + nxt[0][len("next: "):])
        else:
            out.append(f"• Next: unknown ({snap.sections['loops'].reason or 'no next line'})")
        return out
    except Exception as e:
        return [f"Health: unavailable ({type(e).__name__}: {str(e)[:120]})"]


def _trading_lines(user_id: str, data_dir: Optional[str]) -> List[str]:
    """086 W6: the Trading section — the same renderer as ``/trade status``
    (``core.trading_report``), $0, no LLM. Absent with no mandate and no
    position; an unreadable store is named on its line, never "none"."""
    try:
        from core.trading_report import trading_view
        view = trading_view(user_id, now=time.time(), data_dir=data_dir)
    except Exception as e:  # noqa: BLE001 — render the reason, never a silent blank
        return [f"• Trading: unavailable ({type(e).__name__}: {str(e)[:120]})"]
    if not view.get("present"):
        return []
    unknown = view.get("unknown") or []
    head = ("• Trading:" if not unknown
            else f"• Trading (could not read: {', '.join(unknown)}):")
    return [head] + [f"   {ln}" for ln in view.get("lines") or []]


#: An ask open this long is named as possibly obsolete (it led every digest of
#: 10-17..10-29 for a contract the programme no longer used).
ASK_STALE_DAYS = 7


def _ask_age(ask: Dict[str, Any]) -> str:
    """`` (open 12.8 d)`` — and, past ASK_STALE_DAYS, how to close it."""
    if ask.get("created_at") in (None, ""):
        return ""
    try:
        days = (time.time() - float(ask.get("created_at"))) / 86400
    except (TypeError, ValueError) as e:
        return f" (age unknown: {type(e).__name__})"
    text = f" (open {days:.1f} d)" if days >= 1 else ""
    if days >= ASK_STALE_DAYS and ask.get("id"):
        text += f" — still needed? if not, tell me to withdraw ask {ask['id']}"
    return text


async def compose_digest(user_id: str, *, days: int = 1,
                         data_dir: Optional[str] = None, db=None) -> str:
    """Build the digest text from evidence. Pure-ish (all reads fail-open)."""
    since_ts = time.time() - max(1, int(days)) * 86400
    failures: List[str] = []
    snap = _OneSnapshot(user_id, data_dir, days)   # built once, on first use
    ledger = _read(failures, "ledger", _ledger, user_id, days)
    agg = _read(failures, "event log", _event_aggregate, user_id, since_ts)
    asks = _read(failures, "open asks", _open_asks, user_id, data_dir, snap)
    pending = _read(failures, "pending approvals", _pending_items, user_id, data_dir, snap)
    episodes = _read(failures, "episodes", _episodes, user_id, since_ts)
    top_cost = _read(failures, "AI cost records", _top_cost, user_id, data_dir, days)
    failed_jobs = _read(failures, "job store", _failed_jobs, user_id, data_dir)
    proposals = _read(failures, "curator proposals", _proposals, user_id, since_ts, data_dir)
    mechanism = _read(failures, "reviewer notes", _mechanism_notes, user_id, since_ts, data_dir)

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
    head = period
    try:   # the owner's calendar day (core/owner_time); the window stays rolling
        from core.owner_time import owner_day
        head += f", {owner_day()[0]}"
    except Exception as e:  # noqa: BLE001 — a label, never a reason to drop the digest
        _logger.debug("digest: owner day unreadable (%s)", type(e).__name__)
    lines = [f"Daily digest — {head}:"]
    lines.extend(_failed_job_lines(failed_jobs, since_ts))
    # 2026-08-28 status SSOT: the digest leads with the same health block
    # /status renders. A degraded instance is the first thing the owner reads;
    # a builder failure is one honest line, never a silently clean digest.
    lines.extend(_health_lines(user_id, data_dir, snap))
    lines.append(f"• Activity: {sessions} session(s), {_n(n_goals, agg is None)} goal event(s), "
                 f"{_n(n_self_mods, agg is None)} self-change(s)")
    lines.extend(f"• {p}" for p in (proposals or [])[:PROPOSALS_MAX_LINES])
    lines.extend(f"• For the developer: {n[:200]}" for n in (mechanism or [])[:PROPOSALS_MAX_LINES])
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

    lines.extend(_trading_lines(user_id, data_dir))

    if asks is None:
        lines.append("• Pending approvals: could not read the open asks (see above)")
    elif asks or pending:
        lines.append(f"• Pending approvals ({len(asks) + len(pending or [])}"
                     + ("+" if pending is None else "") + "):")
        for a in asks[:5]:
            title = (a.get("title") or "").strip().replace("\n", " ")[:80]
            lines.append(f"   - {title}{_ask_age(a)}")
        for it in (pending or [])[:5]:
            lines.append(f"   - [{it.get('kind') or '?'}] {it.get('preview') or it.get('id') or ''}"
                         f" — /pending")
    elif pending is None:
        lines.append("• Pending approvals: no open asks; /pending could not be read (see above)")
    else:
        lines.append("• Pending approvals: none")
    lines.extend(_missed_lines(user_id, data_dir, snap))
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
