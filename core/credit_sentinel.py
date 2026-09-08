"""§6.3 provider-credit sentinel — no more silent multi-day 402 grind.

Live evidence (2026-07-07): 465× OpenRouter 402 in one day, zero owner-facing
signal, autonomy effectively dead for two days while cron/goal tickers kept
grinding paid-looking runs. The sentinel turns a credit-death refusal from an
autonomous run into:

1. ONE safety-net notice through the §3.2 delivery rail (rail dedup + the
   already-active check make it once per episode);
2. a durable file latch (``<data_root>/CREDIT_SENTINEL``, JSON with the trip
   timestamp) that pauses goal dispatch and LLM cron ticks — $0 ticks
   (digest, wake_agent=false) keep flowing;
3. AUTO-RELEASE after ``CREDIT_SENTINEL_RELEASE_HOURS`` — one paid probe per
   window instead of a permanent manual halt (`rm` the file or set the env
   flag off to release early).

Modeled on the ``AUTONOMY_HALT`` latch (constants.autonomy_halted) but
time-bounded and self-releasing. Everything here is fail-open.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Optional

from core.env import bool_env, int_env

logger = logging.getLogger(__name__)

SENTINEL_FILENAME = "CREDIT_SENTINEL"

_CREDIT_DEATH_MARKERS = (
    "insufficient_quota",
    "insufficient credits",
    "payment required",
    "billing",
    "credit balance",
    # Plan-quota death dressed as a 429: z.ai GLM Coding Plan code 1310
    # ("Weekly/Monthly Limit Exhausted. Your limit will reset at <ts>") and its
    # pay-as-you-go sibling. Without these the 2026-08 storm never latched: the
    # dispatcher ground 63 dead sessions/day against an account that could not
    # serve until its weekly reset.
    "limit exhausted",
    "insufficient balance",
)

# "402" needs word boundaries, not bare-substring matching: the bare form
# tripped on formatted spend amounts ("$0.4020"), token counts ("requested
# 130402 tokens") and hex request ids ("req_9f402ab13c7e") — the 2026-07-18
# sentinel false-positive class, reproduced again by the 2026-07-23 validation
# in the chain-aware classifier (a coincidental chained "402" flipped a
# retryable step fatal with billing failover off). \b keeps every real
# provider shape matching: "Error code: 402", "HTTP/1.1 402", "(402)",
# "status_code=402" all carry non-word neighbors.
_CREDIT_DEATH_402_RE = re.compile(r"\b402\b")


def credit_sentinel_enabled() -> bool:
    return bool_env("CREDIT_SENTINEL_ENABLED", True)


def _release_hours() -> int:
    return int_env("CREDIT_SENTINEL_RELEASE_HOURS", 6)


def _sentinel_path() -> str:
    try:
        from core.runtime_config import get_data_root
        base = get_data_root()
    except Exception:
        # Fallback mirrors the SSOT precedence (resolve_data_home: POLYROB_DATA_DIR
        # → cwd/.polyrob), NOT the legacy "POLYROB_DATA_DIR or DATA_ROOT or 'data'"
        # order — DATA_ROOT is the SESSION-tree axis, and a sentinel latched there
        # is a safety gate reading a different file than the agent writes
        # (structural audit T3, 2026-07-16).
        try:
            from core.runtime_paths import resolve_data_home
            base = str(resolve_data_home())
        except Exception:
            base = os.getenv("POLYROB_DATA_DIR") or "data"
    return os.path.join(base, SENTINEL_FILENAME)


def looks_like_credit_death(text: Optional[str]) -> bool:
    """Does a run-refusal status / exception string look like provider credit
    death? Called on framework status strings ("Session failed: PERMANENT
    ERROR: … 402 …"), not on agent prose."""
    if not text:
        return False
    low = str(text).lower()
    if any(m in low for m in _CREDIT_DEATH_MARKERS):
        return True
    return bool(_CREDIT_DEATH_402_RE.search(low))


#: Cap on how far in the future a provider-stated reset may push the latch —
#: a garbled/mis-parsed timestamp must not pause autonomy for a month.
_MAX_RELEASE_AHEAD_SEC = 7 * 86400

#: Providers rarely state a timezone in "reset at <ts>". Bias EARLY: a
#: too-early release costs one failed probe (which re-trips the latch); a
#: too-late one keeps autonomy dead after the account already recovered.
#: z.ai stamps Beijing time (UTC+8), so that reading is tried first.
_TS_CANDIDATE_OFFSETS_SEC = (8 * 3600, 0)

_RESET_AT_RE = re.compile(
    r"reset at (\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")


def extract_reset_ts(text: Optional[str]) -> Optional[float]:
    """Parse a provider-stated quota-reset time ("Your limit will reset at
    2026-08-18 18:01:49") into an epoch release timestamp, or None.

    Returns the earliest plausible reading that is still meaningfully in the
    future (see ``_TS_CANDIDATE_OFFSETS_SEC``), capped at
    ``_MAX_RELEASE_AHEAD_SEC``. None ⇒ callers fall back to the fixed
    ``CREDIT_SENTINEL_RELEASE_HOURS`` window.
    """
    if not text:
        return None
    m = _RESET_AT_RE.search(str(text))
    if not m:
        return None
    try:
        import calendar
        from datetime import datetime
        dt = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
        as_utc = float(calendar.timegm(dt.timetuple()))
    except Exception:
        return None
    now = time.time()
    for offset in _TS_CANDIDATE_OFFSETS_SEC:
        cand = as_utc - offset
        if cand > now + 900:
            return min(cand, now + _MAX_RELEASE_AHEAD_SEC)
    return None


#: Latch key for a credit death we could not attribute to a provider. It pauses
#: everything, which is the conservative reading of "we don't know what died".
GLOBAL_KEY = "*"


def _read_latch(path: str) -> dict:
    """The latch as ``{provider: {"ts": float, "release_ts": float|None}}``.
    Tolerates every historical shape.

    A latch written before provider scoping is ``{"ts":…, "reason":…}`` with no
    provider. Reading it as GLOBAL (not as "no entries") matters on upgrade —
    the alternative silently un-pauses a genuinely credit-dead provider.
    """
    def _entry(ts: float, release_ts: Optional[float] = None,
               reason: str = "") -> dict:
        return {"ts": ts, "release_ts": release_ts, "reason": reason}

    try:
        with open(path) as f:
            data = json.load(f) or {}
    except Exception:
        # Unreadable latch: treat its mtime as a global trip time.
        return {GLOBAL_KEY: _entry(os.path.getmtime(path))}
    providers = data.get("providers")
    if isinstance(providers, dict):
        out = {}
        for name, entry in providers.items():
            try:
                release = (entry or {}).get("release_ts")
                out[str(name)] = _entry(
                    float((entry or {}).get("ts") or 0.0),
                    float(release) if release else None,
                    str((entry or {}).get("reason") or ""))
            except Exception:
                continue
        return out
    # Legacy unkeyed shape.
    try:
        return {GLOBAL_KEY: _entry(float(data.get("ts") or 0.0))}
    except Exception:
        return {GLOBAL_KEY: _entry(os.path.getmtime(path))}


def _entry_expired(entry: dict, now: float, window: float) -> bool:
    """A provider-stated ``release_ts`` replaces the fixed window entirely."""
    release_ts = entry.get("release_ts")
    if release_ts:
        return now >= float(release_ts)
    return now - float(entry.get("ts") or 0.0) >= window


def credit_sentinel_active(provider: Optional[str] = None) -> bool:
    """Is autonomy paused for *provider*? Expired entries auto-release.

    The latch is PER PROVIDER. Credit death is a property of one account, not
    of the deployment: a dead OpenRouter key must not pause work pinned to a
    healthy provider — least of all a flat-rate seat, where credit death cannot
    occur at all. (Live 2026-08-14: one legacy cron job pinned the dry
    OpenRouter; its 402 latched the global sentinel and paused every goal,
    including correctly-pinned ones, for 5.7 hours.)

    ``provider=None`` keeps the legacy meaning — "is anything paused" — so a
    caller that does not know which provider it is about to use stays
    conservative.
    """
    if not credit_sentinel_enabled():
        return False
    path = _sentinel_path()
    try:
        if not os.path.exists(path):
            return False
        entries = _read_latch(path)
        window = _release_hours() * 3600
        now = time.time()
        fresh = {name: e for name, e in entries.items()
                 if not _entry_expired(e, now, window)}
        if not fresh:
            try:
                os.remove(path)
                logger.info("credit sentinel auto-released after %sh", _release_hours())
            except OSError:
                pass
            return False
        if len(fresh) != len(entries):
            _write_latch(path, fresh)  # drop only the expired providers
        if provider is None:
            return True
        # A global (unattributed) trip pauses every provider.
        return GLOBAL_KEY in fresh or str(provider) in fresh
    except Exception:
        return False  # fail-open: a broken latch never blocks autonomy


def credit_sentinel_status() -> dict:
    """The latch as the status surfaces read it (2026-08-28 status SSOT).

    ``{provider: {"ts", "release_ts", "reason"}}`` for every entry that is still
    FRESH (an expired entry is auto-released exactly as ``credit_sentinel_active``
    does), ``{}`` when clear or disabled. Raises — a caller renders the error as
    ``unavailable (<reason>)``, never as "clear": a latch we cannot read is not a
    latch we know to be open.
    """
    if not credit_sentinel_enabled():
        return {}
    path = _sentinel_path()
    if not os.path.exists(path):
        return {}
    entries = _read_latch(path)
    window = _release_hours() * 3600
    now = time.time()
    return {name: dict(e) for name, e in entries.items()
            if not _entry_expired(e, now, window)}


def _write_latch(path: str, entries: dict, reason: str = "") -> None:
    """Persist ``{provider: entry}``; entries may be the ``_read_latch`` dict
    shape or bare ``float`` timestamps. Never raises."""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        providers = {}
        for name, entry in entries.items():
            if isinstance(entry, dict):
                providers[name] = {
                    "ts": entry.get("ts"),
                    "release_ts": entry.get("release_ts"),
                    "reason": str(entry.get("reason") or reason)[:500],
                }
            else:
                providers[name] = {"ts": entry, "reason": str(reason)[:500]}
        with open(path, "w") as f:
            json.dump({"providers": providers}, f)
    except Exception:
        logger.warning("credit sentinel: latch write failed", exc_info=True)


async def trip_credit_sentinel(reason: str, *, provider: Optional[str] = None,
                               container: Any = None,
                               user_id: str = "",
                               release_ts: Optional[float] = None) -> bool:
    """Activate the latch for *provider* + send the one §3.4 safety-net notice.

    Idempotent while that provider is already latched; never raises. ``provider``
    is the account whose credits died — pass it whenever the caller knows it, so
    a healthy provider keeps serving. Omitted => a GLOBAL trip (pauses
    everything), the conservative reading of an unattributable failure.

    ``release_ts`` (epoch) is a provider-stated quota-reset time (see
    ``extract_reset_ts``): when given, the latch holds until then instead of
    the fixed ``CREDIT_SENTINEL_RELEASE_HOURS`` window — one owner notice for
    the whole outage instead of a re-trip (and a fresh ping) every window.
    """
    if not credit_sentinel_enabled():
        return False
    key = str(provider) if provider else GLOBAL_KEY
    already = credit_sentinel_active(key)
    if not already:
        path = _sentinel_path()
        entries = _read_latch(path) if os.path.exists(path) else {}
        entries[key] = {"ts": time.time(), "release_ts": release_ts,
                        "reason": str(reason)[:500]}
        _write_latch(path, entries, reason=reason)
        try:
            from core.event_log import get_event_log
            get_event_log().record("credit_sentinel", user_id=str(user_id or ""),
                                   source="credit_sentinel",
                                   attrs={"reason": str(reason)[:500]})
        except Exception:
            # The latch file is already written; only the telemetry breadcrumb failed.
            logger.warning("credit_sentinel event not recorded", exc_info=True)
        try:
            import core.surfaces.user_delivery as _ud
            # 020 #1: stamp the trip time into the text so a RE-trip within the
            # rail's 24h content-dedup window is byte-distinct — the recurring
            # 402 produces an identical reason, and the 6h release cycle sits
            # well inside the dedup window, so pauses #2/#3 were silently
            # absorbed (live 2026-07-18/19: 3 trips, 1 delivered notice).
            trip_stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
            if release_ts:
                resume = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(release_ts))
                resume_text = f"at the provider's stated quota reset (~{resume})"
            else:
                resume_text = f"in {_release_hours()}h"
            text = (f"⛔ Autonomy paused ({trip_stamp}): provider credit failure — "
                    f"{str(reason)[:300]}. "
                    f"Goal dispatch and LLM cron ticks resume automatically "
                    f"{resume_text} (or remove {_sentinel_path()} after topping up).")
            outcome = await _ud.deliver_user_message(container, str(user_id or ""), text,
                                                     source="credit_sentinel")
            # 020 #3: a deduped sentinel notice means the owner was NOT told
            # about a genuinely new pause — with the stamp above this should
            # never happen; WARN loudly if it ever does.
            if outcome == "deduped":
                logger.warning(
                    "credit sentinel: pause notice was DEDUPED by the delivery "
                    "rail — owner was not informed of this re-trip (proposal 020)")
        except Exception:
            logger.debug("credit sentinel: notice failed (durable fallback already "
                         "handled by the rail)", exc_info=True)
    return True
