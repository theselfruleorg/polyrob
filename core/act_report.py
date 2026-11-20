"""The owner's report of the acts the agent takes on its own (proposal 122 W1).

The owner preference ``report.acts`` chooses it:

- ``money_only`` (the default): the money notices only (``core.wallet.tx_notify`` — every
  money act at once, with its lane and its reason). Nothing else is sent.
- ``every_act``: the money notices, and every OTHER act the agent took on its own (a post,
  a message, a published page, code it ran) in one report each hour.
- ``digest``: the money notices, and the other acts in one report each day.

A money notice is never held for a report in any mode: it is the critical rail
(``tx_notify`` rule 1). The report reads the act records (the ``external_write`` rows the
effect recorder writes, with ``why``, ``params`` and ``source``) — the same rows the public
"what I did and why" text reads, so the two cannot differ. It adds no second action store.

The report covers the closed period before the tick: a UTC hour, or a UTC day. The last
period sent is kept in the event log itself (``act_report`` rows), so a restart neither
repeats a report nor skips one. An unreadable log is said, never read as "no acts".
"""
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PREF_KEY = "report.acts"
MODES = ("every_act", "money_only", "digest")
DEFAULT_MODE = "money_only"
#: Seconds per report period, per mode. ``money_only`` has no report.
PERIOD_SEC = {"every_act": 3600, "digest": 86400}
#: How often the runtime asks whether a report is due.
INTERVAL_SEC = 300
#: The most act lines one report shows; the rest is counted.
MAX_LINES = 40
SOURCE = "act_report"


def report_mode(user_id: Optional[str], home_dir: Any = None) -> str:
    """The owner's ``report.acts`` (the default when unset or unreadable)."""
    try:
        from core import prefs
        from core.runtime_paths import data_dir_or_home
        value = prefs.resolve("report.acts", user_id, data_dir_or_home(home_dir),
                              default=DEFAULT_MODE)
        return value if value in MODES else DEFAULT_MODE
    except Exception:
        logger.debug("act report: report.acts unreadable; the default applies", exc_info=True)
        return DEFAULT_MODE


def other_acts(user_id: str, since_ts: float, until_ts: float, *,
               data_dir: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    """The agent's own non-money acts in ``[since_ts, until_ts)``, oldest first, for
    *user_id* and the untenanted rows. ``[]`` when there is no log; ``None`` when the log
    cannot be read."""
    from core.effects import EFFECT_CLASSES
    from core.event_kinds import EXTERNAL_WRITE
    from core.event_log import open_event_log
    try:
        log = open_event_log(data_dir)
    except Exception:
        return None
    if log is None:
        return []
    rows = log.rows_where(kind=EXTERNAL_WRITE,
                          effects=[e for e in EFFECT_CLASSES if e != "money"],
                          since_ts=since_ts, user_ids={str(user_id or ""), ""},
                          attrs_in={}, limit=1000)
    if rows is None:
        return None
    out = [r for r in rows
           if float(r.get("ts") or 0) < until_ts and (r.get("attrs") or {}).get("autonomous")]
    return sorted(out, key=lambda r: float(r.get("ts") or 0))


def render(rows: List[Dict[str, Any]], *, period: str) -> str:
    """One report: a line per act — the time (UTC), the act, its outcome, the short params,
    who decided it and why."""
    lines = [f"🧾 What I did on my own in the last {period} ({len(rows)} act"
             f"{'' if len(rows) == 1 else 's'}; money acts were reported at once):"]
    for r in rows[:MAX_LINES]:
        a = r.get("attrs") or {}
        stamp = time.strftime("%H:%M", time.gmtime(float(r.get("ts") or 0)))
        outcome = a.get("outcome") or "ok"
        line = f"• {stamp} {a.get('action') or a.get('tool') or '?'}"
        if outcome != "ok":
            line += f" [{outcome}]"
        if a.get("params"):
            line += f" {a['params']}"
        from core.act_reason import shown_why
        line += f" — {a.get('source') or 'agent'}; why: {shown_why(a.get('why')) or 'not given'}"
        lines.append(line)
    if len(rows) > MAX_LINES:
        lines.append(f"… and {len(rows) - MAX_LINES} more (the act records hold them all).")
    return "\n".join(lines)


def _last_period_end(user_id: str, data_dir: Optional[str]) -> Optional[float]:
    """The end of the newest period a report covered, or None (never sent, or no log)."""
    from core.event_kinds import ACT_REPORT
    from core.event_log import open_event_log
    log = open_event_log(data_dir)
    if log is None:
        return None
    rows = log.query(kind=ACT_REPORT, user_id=str(user_id or ""), limit=1, strict=True)
    if not rows:
        return None
    return float((rows[0].get("attrs") or {}).get("until") or 0) or None


async def tick(container: Any, user_id: str, *, data_dir: Optional[str] = None,
               now: Optional[float] = None, deliver=None) -> str:
    """Send the due report for *user_id*. Returns what happened (``off``, ``not_due``,
    ``empty``, ``sent``, ``unreadable``, ``error``). Never raises."""
    try:
        mode = report_mode(user_id, data_dir)
        period = PERIOD_SEC.get(mode)
        if not period:
            return "off"
        now = time.time() if now is None else now
        until = (int(now) // period) * period
        last = _last_period_end(user_id, data_dir)
        if last is not None and last >= until:
            return "not_due"
        # A first report, or one after a long stop, covers one period only.
        since = max(last or 0.0, float(until - period))
        rows = other_acts(user_id, since, until, data_dir=data_dir)
        if rows is None:
            text = ("🧾 The act report could not be made: the act records are unreadable "
                    "(this is not \"no acts\").")
            outcome = "unreadable"
        elif not rows:
            text, outcome = "", "empty"
        else:
            text = render(rows, period="hour" if mode == "every_act" else "day")
            outcome = "sent"
        if text:
            if deliver is None:
                from core.surfaces.user_delivery import deliver_user_message as deliver
            await deliver(container, user_id, text, source=SOURCE)
        from core.event_kinds import ACT_REPORT
        from core.event_log import emit
        emit(ACT_REPORT, source=SOURCE, user_id=user_id,
             attrs={"mode": mode, "since": since, "until": float(until),
                    "acts": len(rows or []), "outcome": outcome})
        return outcome
    except Exception:
        logger.warning("act report tick failed", exc_info=True)
        return "error"


__all__ = ["DEFAULT_MODE", "INTERVAL_SEC", "MODES", "PERIOD_SEC", "PREF_KEY", "other_acts",
           "render", "report_mode", "tick"]
