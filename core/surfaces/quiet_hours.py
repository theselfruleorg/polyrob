"""Quiet-hours window primitives (018 P0.3 — `digest.quiet_hours` enforcement).

Until this module the pref only whispered into the SELF_CONTEXT style line; no
delivery path consulted it. The one user-bound proactive rail
(`core/surfaces/user_delivery.py`) now calls :func:`quiet_window_active` and
DEFERS (never drops) sends inside the window — owner decision 2026-07-18 —
releasing them at window-end via ``release_quiet_held`` (same module) driven by
the autonomy-runtime ticker. Interactive replies never route through that rail,
so they are never gated.

Window grammar matches the pref's write-time validation: ``"HH-HH"`` local
time, hours 0-23, END-EXCLUSIVE (``"23-08"`` = from 23:00 until 08:00; a send
at 08:00 goes out). A zero-length window (``"8-8"``) parses as None = no
window. All helpers fail open (None/False) — a parse or pref fault must never
hold traffic.
"""
import logging
import re
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_WINDOW_RE = re.compile(r"^\s*(\d{1,2})\s*-\s*(\d{1,2})\s*$")


def parse_quiet_window(raw: object) -> Optional[Tuple[int, int]]:
    """``"23-08"`` -> ``(23, 8)``; invalid/zero-length/None -> ``None``."""
    if not raw or not isinstance(raw, str):
        return None
    m = _WINDOW_RE.match(raw)
    if not m:
        return None
    start, end = int(m.group(1)), int(m.group(2))
    if not (0 <= start <= 23 and 0 <= end <= 23) or start == end:
        return None
    return start, end


def in_quiet_window(hour: int, window: Tuple[int, int]) -> bool:
    """Whether *hour* (0-23) falls inside *window*, wrapping midnight."""
    start, end = window
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _now_hour_local() -> int:
    """Current local hour (seam kept for test monkeypatching)."""
    from datetime import datetime
    return datetime.now().hour


def effective_quiet_hours(user_id, home_dir) -> Optional[str]:
    """The tenant's ``digest.quiet_hours`` pref (no env backing — a pure
    preference; override merge). None = no window configured."""
    try:
        from core import prefs
        out = prefs.resolve("digest.quiet_hours", user_id, home_dir,
                            env_value=None, default=None)
        return out if isinstance(out, str) and out.strip() else None
    except Exception:
        logger.debug("quiet_hours: pref resolution failed (fail-open)",
                     exc_info=True)
        return None


def quiet_window_active(user_id, home_dir) -> bool:
    """True iff the tenant configured a quiet window and local time is inside it."""
    window = parse_quiet_window(effective_quiet_hours(user_id, home_dir))
    if window is None:
        return False
    return in_quiet_window(_now_hour_local(), window)
