"""The owner's clock (116 W8): one zone, one way to show a time in it.

The server clock is UTC on prod and the owner lives elsewhere, so a time the agent
or a status seat shows in UTC only is read as the owner's own time — the agent told
the owner yesterday's date (116 P15). The ``owner.timezone`` pref (old name
``digest.timezone``, still read) is the ONE zone; this module is the ONE place that
resolves it and the ONE formatter of a time in it.

- :func:`owner_zone` — the IANA name, or None (no zone set: everything stays UTC).
- :func:`stamp` — ``11-11 18:03Z (01:03 +07)`` for a status line; the UTC part keeps
  the caller's format, so a seat that showed UTC still shows the same UTC text first.
- :func:`owner_clock` — ``2026-11-12 09:10 Asia/Bangkok (UTC+7)`` for the prompt.
- :func:`owner_day` — the owner's calendar day (date and bounds) for a reader that
  means a DAY (the digest header). A gate stays a rolling 24 h and says "24h".

Cron stays on the server clock. Every helper fails open to the UTC form: a bad pref
or an unknown zone never breaks a render. Core tier: stdlib + ``core.prefs`` (lazy).
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

#: The pref key (IANA name). ``core.prefs.PREF_ALIASES`` maps the old name to it.
TIMEZONE_PREF = "owner.timezone"

#: ``zone=AUTO`` (the default) resolves the owner's zone; pass a name, or None for UTC.
AUTO = object()


def owner_zone(user_id=None, home=None) -> Optional[str]:
    """The owner's ``owner.timezone`` pref, or None (no zone: the server clock).

    With no ``user_id`` and no ``home`` it reads the instance owner's scope
    (``core.prefs.owner_pref_scope``); with a ``user_id`` and no ``home`` it reads that
    tenant in the prefs home. Fail-open: an unreadable pref is None, logged at DEBUG.
    """
    try:
        from core import prefs
        if user_id is None and not home:
            user_id, home = prefs.owner_pref_scope()
        elif not home:
            from core.runtime_paths import prefs_home_dir
            home = prefs_home_dir()
        if not home:
            return None
        out = prefs.resolve("owner.timezone", user_id, home, env_value=None, default=None)
        return out.strip() if isinstance(out, str) and out.strip() else None
    except Exception:
        logger.debug("owner_time: timezone pref resolution failed (fail-open)", exc_info=True)
        return None


def _zone(zone) -> Optional[str]:
    return owner_zone() if zone is AUTO else zone


def _to_ts(ts) -> Optional[float]:
    """Epoch seconds from a float/int/str, or an aware datetime; a NAIVE datetime is the
    server clock (cron's ``next_run_at``). None when it is missing or unreadable."""
    if ts is None or ts == "" or ts == 0:
        return None
    try:
        return ts.timestamp() if isinstance(ts, datetime) else float(ts)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def in_zone(ts, zone: Optional[str]) -> Optional[datetime]:
    """*ts* as an aware datetime in *zone*, or None (no zone, unknown zone, bad ts)."""
    sec = _to_ts(ts)
    if not zone or sec is None:
        return None
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(sec, ZoneInfo(zone))
    except Exception:
        logger.debug("owner_time: unknown timezone %r — UTC only", zone)
        return None


def _offset(local: datetime, *, sep: str) -> str:
    """``+07`` / ``+05:30`` (``sep=""``) or ``UTC+7`` / ``UTC+5:30`` (``sep="UTC"``)."""
    minutes = int((local.utcoffset() or timedelta(0)).total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    h, m = divmod(abs(minutes), 60)
    if sep:
        return f"UTC{sign}{h}" + (f":{m:02d}" if m else "")
    return f"{sign}{h:02d}" + (f":{m:02d}" if m else "")


def stamp(ts, zone=AUTO, *, utc_format: str = "%m-%d %H:%MZ", missing: str = "?") -> str:
    """One time for a status line: the UTC form first, then the owner's clock.

    ``stamp(ts)`` → ``11-11 18:03Z (01:03 +07)``; when the owner's DATE differs from the
    UTC date it is named: ``11-11 18:03Z (11-12 01:03 +07)``. No zone set, an unknown
    zone or a zero offset → the plain UTC form, byte-equal to ``time.strftime(utc_format,
    time.gmtime(ts))``. A missing (None, "", 0) or unreadable ts → *missing*.
    ``zone``: AUTO (default) = the owner's zone; a name; None = UTC only. A seat that
    stamps many times resolves the zone once (``owner_zone()``) and passes it.
    """
    sec = _to_ts(ts)
    if sec is None:
        return missing
    try:
        utc = time.strftime(utc_format, time.gmtime(sec))
    except (ValueError, OverflowError, OSError):
        return missing
    local = in_zone(sec, _zone(zone))
    if local is None or not local.utcoffset():
        return utc
    day = "" if local.date() == datetime.fromtimestamp(sec, timezone.utc).date() \
        else local.strftime("%m-%d ")
    return f"{utc} ({day}{local.strftime('%H:%M')} {_offset(local, sep='')})"


def owner_clock(now=None, zone=AUTO) -> str:
    """``2026-11-12 09:10 Asia/Bangkok (UTC+7)``, or "" when no zone is set (or it is
    unknown). *now* defaults to the current time."""
    local = in_zone(time.time() if now is None else now, _zone(zone))
    if local is None:
        return ""
    return f"{local.strftime('%Y-%m-%d %H:%M')} {local.tzinfo} ({_offset(local, sep='UTC')})"


def owner_day(now=None, zone=AUTO) -> Tuple[str, float, float]:
    """The owner's calendar day around *now*: ``(YYYY-MM-DD, start_ts, end_ts)``, end
    exclusive. No zone → the UTC day. For a reader that means a DAY, never a gate."""
    sec = _to_ts(time.time() if now is None else now) or time.time()
    local = in_zone(sec, _zone(zone)) or datetime.fromtimestamp(sec, timezone.utc)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    nxt = start + timedelta(days=1)  # wall-clock step: a DST day is 23 h or 25 h
    return start.strftime("%Y-%m-%d"), start.timestamp(), nxt.timestamp()
