"""The ``tools`` status section (058 T4.2) — usage per tool, from the
``tool_invoked`` event (T4.1). "No record" is distinguished from zero.

Three invariants, each with a specific trap:

1. The log is opened through ``core.event_log.open_event_log()``, which returns
   ``None`` for an absent file. ``TelemetryEventLog(...)`` CREATES it, and a
   status read must never create a db.
2. A tool with no row renders ``never invoked (recording since <date>)``, not
   ``0``. "No record since we started recording" is a different fact from
   "zero", and the confident version is exactly what the snapshot forbids.
3. Every failure names its reason (``tests/test_status_silence_ratchet.py``
   forbids a new silent ``except`` in a status render path).

Layering: this module reads the tool list from ``core/tool_capabilities.py``
(the classification SSOT — 36 ids), never ``tools/descriptors.py``; ``core``
may not import the tools tier. That is also why there is NO per-tool flag
column here: enablement is decided by ``tools/descriptors`` gates and the
container, which this tier cannot see. The console's Capabilities tab and
``polyrob doctor --flags`` answer "is it on"; this section answers "is it used".
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from core.status_snapshot import STATE_OK, Section, _rows

WINDOW_DAYS = 30
_MAX_USED_LINES = 8
_MAX_NEVER_NAMES = 12


def _day(ts: Optional[float]) -> str:
    if ts is None:
        return "?"
    return time.strftime("%Y-%m-%d", time.gmtime(float(ts)))


def _hhmm_day(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.gmtime(float(ts)))


def _append_absent_extras(sec) -> None:
    """What this INSTALL cannot do, beside what it has used (062).

    ⚠️ APPENDED, never prepended: the first line of this section is the usage
    headline that several seats and tests read positionally.
    """
    try:
        from core.install_facts import missing_extras
        absent = missing_extras()
        sec.data["absent_extras"] = [e for e, _c, _r in absent]
        if absent:
            sec.lines.append(
                "not installed: " + ", ".join(e for e, _c, _r in absent)
                + " — `polyrob doctor` names the command for each")
    except Exception as exc:
        sec.data["absent_extras"] = None
        sec.lines.append(f"optional extras unreadable ({type(exc).__name__})")


def tools_section(user_id: str, data_dir: str, now: Optional[float] = None) -> Section:
    from core.event_log import open_event_log
    from core.tool_capabilities import TOOL_CAPABILITIES

    now = float(now if now is not None else time.time())
    known = sorted(TOOL_CAPABILITIES)
    sec = Section(name="tools", data={"window_days": WINDOW_DAYS, "known": len(known),
                                      "tools": {}, "recording_since": None})

    log = open_event_log(data_dir)  # None = the file does not exist; never create it
    if log is None:
        sec.lines.append(f"no telemetry log yet — tool usage unrecorded ({len(known)} tools known)")
        sec.data["reason"] = "no telemetry log"
        _append_absent_extras(sec)
        return sec

    path = getattr(log, "db_path", None) or getattr(log, "path", None)
    if not path:
        from core.event_log import telemetry_db_path
        path = telemetry_db_path(data_dir)
    since = now - WINDOW_DAYS * 86400
    first = _rows(path, "SELECT MIN(ts) AS t FROM telemetry_events WHERE kind = 'tool_invoked'")
    recording_since = first[0]["t"] if first and first[0].get("t") is not None else None
    sec.data["recording_since"] = recording_since
    if recording_since is None:
        sec.lines.append(f"no tool_invoked rows yet — usage unrecorded ({len(known)} tools known)")
        _append_absent_extras(sec)
        return sec

    rows = _rows(
        path,
        "SELECT ts, attrs FROM telemetry_events WHERE kind = 'tool_invoked' "
        "AND ts >= ? AND (user_id = ? OR user_id = '') ORDER BY ts DESC LIMIT 200000",
        (since, str(user_id)))
    per: Dict[str, Dict[str, Any]] = {}
    unparseable = 0
    for r in rows:
        attrs: Dict[str, Any] = {}
        try:
            attrs = json.loads(r.get("attrs") or "{}") or {}
            tool = str(attrs.get("tool") or "unknown")
        except Exception:
            unparseable += 1  # counted and surfaced, never dropped
            tool = "unknown"
        slot = per.setdefault(tool, {"count": 0, "last": None, "errors": 0})
        slot["count"] += 1
        ts = float(r["ts"])
        if slot["last"] is None or ts > slot["last"]:
            slot["last"] = ts
        if isinstance(attrs, dict) and attrs.get("ok") is False:
            slot["errors"] += 1
    sec.data["tools"] = per
    sec.data["unparseable"] = unparseable

    used_known = [t for t in known if t in per]
    never = [t for t in known if t not in per]
    sec.lines.append(
        f"{len(used_known)} of {len(known)} tools invoked in {WINDOW_DAYS} d "
        f"(recording since {_day(recording_since)})")
    ranked = sorted(((t, d) for t, d in per.items()), key=lambda kv: -kv[1]["count"])
    for tool, d in ranked[:_MAX_USED_LINES]:
        err = f", {d['errors']} err" if d["errors"] else ""
        sec.lines.append(f"{tool}: {d['count']} ×{err} · last {_hhmm_day(d['last'])}")
    if len(ranked) > _MAX_USED_LINES:
        sec.lines.append(f"… and {len(ranked) - _MAX_USED_LINES} more tools used")
    if never:
        names = ", ".join(never[:_MAX_NEVER_NAMES])
        more = f" +{len(never) - _MAX_NEVER_NAMES} more" if len(never) > _MAX_NEVER_NAMES else ""
        sec.lines.append(
            f"never invoked (recording since {_day(recording_since)}): {names}{more}")
    if unparseable:
        sec.lines.append(f"{unparseable} tool_invoked row(s) unparseable — counted under 'unknown'")
    _append_absent_extras(sec)
    return sec
