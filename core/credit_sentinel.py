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
    "please recharge",
    "add credits",
)

# "402" must stand alone as a status code. Bare-substring matching tripped on spend
# amounts ("$0.4020", "$0.402"), token counts ("requested 130402 tokens", "1,402") and
# hex request ids ("req_9f402ab13c7e"). Every real provider shape keeps matching:
# "Error code: 402", "HTTP/1.1 402", "(402)", "status_code=402".
_CREDIT_DEATH_402_RE = re.compile(r"(?<![\w.,$])402(?!\w|[.,]\d)")


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


#: A 402 that says "wait", not "pay". OpenRouter refuses a request whose
#: reservation, ADDED to the account's other in-flight requests, passes the
#: balance (``metadata.reason = in_flight_budget_exceeded``). The account is
#: funded and the request succeeds once a sibling settles — prod 2026-10-12 read
#: it as credit death, paused autonomy for 6h and failed a goal over to a dead
#: provider.
_TRANSIENT_BUDGET_MARKERS = (
    "in_flight_budget",
    "in-flight requests",
)


def looks_like_transient_budget(text: Optional[str]) -> bool:
    """Is this a 402 that clears by itself once concurrent requests settle?
    Callers treat it as a rate limit (back off, retry), never as credit death."""
    if not text:
        return False
    low = str(text).lower()
    return any(m in low for m in _TRANSIENT_BUDGET_MARKERS)


def looks_like_credit_death(text: Optional[str]) -> bool:
    """Does a run-refusal status / exception string look like provider credit
    death? Called on framework status strings ("Session failed: PERMANENT
    ERROR: … 402 …"), not on agent prose."""
    if not text:
        return False
    if looks_like_transient_budget(text):
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


#: 098 CO-11: per-provider latch machine. ``latched -> latched`` is a re-trip
#: (a fresh provider-stated release time), ``latched -> clear`` a release.
from core.state.machine import StateMachine  # noqa: E402

LATCH_MACHINE = StateMachine(
    name="credit_latch", states=frozenset({"clear", "latched"}),
    transitions={"clear": frozenset({"latched"}),
                 "latched": frozenset({"latched", "clear"})},
    initial=frozenset({"clear"}))

_UNREADABLE = object()
_unreadable_logged = False


def _entry(ts: float, release_ts: Optional[float] = None, reason: str = "",
           last_recovery_check: Optional[float] = None) -> dict:
    out = {"ts": ts, "release_ts": release_ts, "reason": reason}
    if last_recovery_check is not None:
        out["last_recovery_check"] = last_recovery_check
    return out


def _parse(data: Any, path: str) -> dict:
    """``{provider: {"ts", "release_ts", "reason"}}`` from the file's JSON.
    Tolerates every historical shape.

    A latch written before provider scoping is ``{"ts":…, "reason":…}`` with no
    provider. Reading it as GLOBAL (not as "no entries") matters on upgrade —
    the alternative silently un-pauses a genuinely credit-dead provider.
    Writes are atomic (098 CO-11), so an unparsable file is real corruption,
    never a torn write: it reads as a GLOBAL trip (logged once) until
    ``release(None)`` resets it.
    """
    global _unreadable_logged
    if data is _UNREADABLE or not isinstance(data, dict):
        if not _unreadable_logged:
            _unreadable_logged = True
            logger.warning("credit_sentinel.unreadable %s — read as a global trip; "
                           "release every provider to reset it", path)
        try:
            return {GLOBAL_KEY: _entry(os.path.getmtime(path))}
        except OSError:
            return {GLOBAL_KEY: _entry(time.time())}
    providers = data.get("providers")
    if isinstance(providers, dict):
        out = {}
        for name, entry in providers.items():
            try:
                release = (entry or {}).get("release_ts")
                checked = (entry or {}).get("last_recovery_check")
                out[str(name)] = _entry(
                    float((entry or {}).get("ts") or 0.0),
                    float(release) if release else None,
                    str((entry or {}).get("reason") or ""),
                    float(checked) if checked is not None else None)
            except Exception:
                continue
        return out
    if not data:
        return {}
    # Legacy unkeyed shape.
    try:
        return {GLOBAL_KEY: _entry(float(data.get("ts") or 0.0))}
    except Exception:
        return {GLOBAL_KEY: _entry(os.path.getmtime(path))}


def _load(path: str) -> Any:
    from core.state.files import read_json
    if not os.path.exists(path):
        return {}
    return read_json(path, _UNREADABLE)


def _read_latch(path: str) -> dict:
    """The latch entries as stored now (no lock: one atomic file read)."""
    return _parse(_load(path), path)


def _mutate(fn) -> Any:
    """``fn(entries) -> (new_entries | None, result)`` under the latch's file
    lock; ``None`` writes nothing. An empty latch is written as
    ``{"providers": {}}`` — never ``os.remove``, which no lock can fence."""
    from core.state.files import atomic_write_json, file_lock
    path = _sentinel_path()
    with file_lock(path):
        entries = _parse(_load(path), path)
        new, result = fn(entries)
        if new is not None:
            atomic_write_json(path, {"providers": _serialize(new)}, indent=None)
        return result


def _serialize(entries: dict, reason: str = "") -> dict:
    out = {}
    for name, entry in entries.items():
        if isinstance(entry, dict):
            out[name] = {"ts": entry.get("ts"), "release_ts": entry.get("release_ts"),
                         "reason": str(entry.get("reason") or reason)[:500]}
            if entry.get("last_recovery_check") is not None:
                out[name]["last_recovery_check"] = entry.get("last_recovery_check")
        else:
            out[name] = {"ts": entry, "reason": str(reason)[:500]}
    return out


def _fresh(entries: dict, now: float) -> dict:
    window = _release_hours() * 3600
    return {n: e for n, e in entries.items() if not _entry_expired(e, now, window)}


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
        now = time.time()
        entries = _read_latch(path)
        fresh = _fresh(entries, now)
        if len(fresh) != len(entries):
            # CO-11: the prune re-reads under the lock and drops only what is
            # expired THEN — a trip written meanwhile survives.
            def _prune(cur):
                keep = _fresh(cur, now)
                return (keep if len(keep) != len(cur) else None), keep
            fresh = _mutate(_prune)
            if not fresh:
                logger.info("credit sentinel auto-released after %sh", _release_hours())
        if not fresh:
            return False
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
    return {name: dict(e) for name, e in _fresh(_read_latch(path), time.time()).items()}


def _write_latch(path: str, entries: dict, reason: str = "") -> None:
    """Persist ``{provider: entry}`` atomically under the latch lock (a test and
    migration seam; production writes go through :func:`_mutate`). Never raises."""
    try:
        from core.state.files import atomic_write_json, file_lock
        with file_lock(path):
            atomic_write_json(path, {"providers": _serialize(entries, reason)}, indent=None)
    except Exception:
        logger.warning("credit sentinel: latch write failed", exc_info=True)


def release(provider: Optional[str] = None) -> bool:
    """Drop the latch for *provider* (or every provider) and say whether it did.

    ⚠️ The window exists so an unattended night costs one paid probe per
    release window instead of a permanent halt. It is NOT a penalty: the moment
    the account is topped up the reason is gone, and waiting out the remaining
    hours keeps the agent dead for nothing (2026-09-24, a 06:22Z trip against a
    12:22Z auto-release).

    Per provider, because the latch is. The documented remedy used to be
    ``rm <data>/CREDIT_SENTINEL``, which also releases an account that is still
    dry — the next dispatch then spends a paid probe rediscovering that.

    Returns True only when something was actually released. Releasing
    ``"openrouter"`` while a GLOBAL trip is in force returns False: that trip
    still pauses openrouter, and a True there would tell the caller the
    provider is free when the next dispatch still refuses.
    ``release(None)`` also resets a corrupt latch file.
    """
    if not credit_sentinel_enabled():
        return False
    path = _sentinel_path()
    try:
        if not os.path.exists(path):
            return False

        def _drop(entries):
            if provider is None:
                return ({}, True) if entries else (None, False)
            if str(provider) not in entries:
                return None, False
            return {k: v for k, v in entries.items() if k != str(provider)}, True

        done = _mutate(_drop)
        if done:
            logger.info("credit sentinel released for %s", provider or "every provider")
        return bool(done)
    except Exception:
        logger.warning("credit sentinel: release failed", exc_info=True)
        return False  # fail-open: a release we could not do is not a claim we made


def claim_recovery_check(provider: str, *, now: Optional[float] = None,
                         interval: float = 300.0) -> bool:
    """098 CO-11: claim the right to run one free recovery probe for *provider*.

    The probe cadence lives in the latch doc (``last_recovery_check`` on the
    provider's entry), stamped under the latch lock, so every unit on one data
    home (agent, email, webview) shares ONE cadence instead of each probing on
    its own per-process clock. True = the caller may probe now (the stamp is
    already written); False = not latched, or probed within *interval*.
    Never raises (a claim we could not record is not a claim made).
    """
    if not credit_sentinel_enabled():
        return False
    key = str(provider or "")
    t = time.time() if now is None else float(now)
    try:
        if not key or not os.path.exists(_sentinel_path()):
            return False

        def _claim(entries):
            entry = entries.get(key)
            if not isinstance(entry, dict):
                return None, False
            last = entry.get("last_recovery_check")
            if last is not None and t - float(last) < interval:
                return None, False
            entries = dict(entries)
            entries[key] = dict(entry, last_recovery_check=t)
            return entries, True

        return bool(_mutate(_claim))
    except Exception:
        logger.warning("credit sentinel: recovery-check claim failed", exc_info=True)
        return False


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
    # The notice dedups on "this key was already paused" (a global trip counts).
    already = credit_sentinel_active(key)
    now = time.time()

    def _trip(entries):
        # CO-11: "already latched" is decided PER KEY, under the lock — a
        # provider trip is recorded even under a global one, so releasing the
        # global trip later does not resume a provider that is still dry.
        if key in _fresh(entries, now):
            return None, False
        entries = dict(entries)
        entries[key] = {"ts": now, "release_ts": release_ts, "reason": str(reason)[:500]}
        return entries, True

    try:
        _mutate(_trip)
    except Exception:
        logger.warning("credit sentinel: latch write failed", exc_info=True)
    if not already:
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
