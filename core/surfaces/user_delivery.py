"""§3.2 — ONE user-bound delivery rail with a memory.

All user-bound sends converge here: the agent's ``send_message`` from
autonomous sessions (§3.1), cron delivery's telegram leg, and the framework
safety-net notices (§3.4). The rail adds what the scattered rails never had:

- **content-hash dedup** (24h window, per tenant) — the watermark-spam class;
- **per-tenant rate limit + daily cap** — blast-radius bound: an injected turn
  can at most rate-limited-message its OWN user;
- **durable owner_notice fallback** when no live sink exists or the send fails
  (extends ``push_owner_message``'s T4-04 fallback), so a REPL/local owner
  never silently loses a message.

Recipient resolution is per-tenant and CANONICAL here
(``resolve_telegram_recipient``: ``user_directory`` service →
digit-uid-IS-chat-id (telegram-origin sessions) → owner-principal fallback
(single-user instances)); ``cron/delivery._owner_telegram`` delegates to it. A
session may message its OWN principal only — arbitrary recipients stay the
``message`` tool's job with its own gating.

Dedup/rate state lives in the durable telemetry event log (WAL sqlite), so it
survives restarts; with the event log disabled the gates fail open (send).
Every attempt is recorded (kind="user_delivery") for observability.
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Optional

from core.env import bool_env, int_env
from core.event_kinds import OWNER_NOTICE, USER_DELIVERY

logger = logging.getLogger(__name__)

DELIVERY_EVENT_KIND = USER_DELIVERY

# Outcomes that consumed the content (count toward dedup/rate windows).
_CONSUMED_OUTCOMES = ("sent", "fallback")

# --- priority lanes (2026-07-20 overnight incident) ------------------------
# The cap was a flat FIFO across every source, so whoever spoke FIRST won the
# day regardless of what they had to say. Live consequence on 2026-07-19: the
# 30 slots were spent by 17:33Z on routine chatter, and for the next 12h every
# goal completion (99), both daily digests, and — at 01:38:15Z — the credit
# sentinel's own "autonomy paused" notice were dropped. The owner messaged Rob
# at 02:35Z not knowing it had stopped. Two lanes close that class.
PRIORITY_CRITICAL = "critical"
PRIORITY_NORMAL = "normal"
PRIORITY_LOW = "low"

#: Sources whose messages are safety-bearing: the owner learning that autonomy
#: STOPPED is never optional, so these bypass the daily cap and the hourly rate
#: limit. They stay subject to content dedup, so a byte-identical re-trip can't
#: become a spam channel (proposal 020 stamps the trip time to keep genuine
#: re-trips distinct). Quiet hours still apply — that gate DEFERS rather than
#: drops, so the notice survives either way.
_CRITICAL_SOURCES = frozenset({"credit_sentinel", "halt", "security"})


def _reserved_slots() -> int:
    """Slots of the daily cap that ``priority="low"`` traffic may not consume."""
    return int_env("USER_DELIVERY_RESERVED_SLOTS", 8)


def resolve_priority(source: str, priority: Optional[str]) -> str:
    """Explicit *priority* wins; otherwise derive it from *source*.

    Deriving from source means the credit sentinel needed no call-site change —
    it already sends with ``source="credit_sentinel"``.
    """
    if priority in (PRIORITY_CRITICAL, PRIORITY_NORMAL, PRIORITY_LOW):
        return priority
    if str(source or "") in _CRITICAL_SOURCES:
        return PRIORITY_CRITICAL
    return PRIORITY_NORMAL


def effective_cap_for_priority(cap: int, priority: str) -> int:
    """The daily allowance this *priority* may spend out of *cap*.

    ``low`` gets ``cap - reserved`` so chatter leaves headroom for completions
    and the digest; the reserve is clamped to leave at least one slot, so a
    misconfigured ``USER_DELIVERY_RESERVED_SLOTS >= cap`` can never silence low
    traffic entirely. Every other priority keeps the full cap, which is what
    makes this a pure demotion of explicitly-low traffic rather than a new
    restriction on existing senders.
    """
    if priority != PRIORITY_LOW:
        return cap
    reserved = max(0, min(_reserved_slots(), max(0, cap - 1)))
    return max(0, cap - reserved)


def send_message_user_delivery_enabled() -> bool:
    """§3.1 gate: route autonomous send_message through the rail (default ON)."""
    return bool_env("SEND_MESSAGE_USER_DELIVERY", True)


def _dedup_hours() -> int:
    return int_env("USER_DELIVERY_DEDUP_HOURS", 24)


def _rate_per_hour() -> int:
    return int_env("USER_DELIVERY_RATE_PER_HOUR", 10)


def _daily_cap() -> int:
    return int_env("USER_DELIVERY_DAILY_CAP", 30)


def effective_rate_per_hour(user_id: Optional[str], home_dir) -> int:
    """Owner's proactive-message rate limit: pref (min-merged, spec
    ``delivery.rate_per_hour``) over the ``USER_DELIVERY_RATE_PER_HOUR`` env
    default. No pref file present => byte-identical to ``_rate_per_hour()``
    (owner-UX P1 T4)."""
    from core import prefs
    env_value = _rate_per_hour()
    return prefs.resolve("delivery.rate_per_hour", user_id, home_dir,
                         env_value=env_value, default=env_value)


def effective_daily_cap(user_id: Optional[str], home_dir) -> int:
    """Owner's proactive-message daily cap: pref (min-merged, spec
    ``delivery.daily_cap``) over the ``USER_DELIVERY_DAILY_CAP`` env default.
    No pref file present => byte-identical to ``_daily_cap()`` (owner-UX P1 T4)."""
    from core import prefs
    env_value = _daily_cap()
    return prefs.resolve("delivery.daily_cap", user_id, home_dir,
                         env_value=env_value, default=env_value)


def _home_dir_for_container(container: Any) -> str:
    """Data-home for pref resolution, reusing the SAME data_dir the container's
    BotConfig already carries (no new global default). Fail-open to the resolved
    data home (WS-3: never a relative "data" under the cwd) when no container/config
    is available — the test-fixture containers in this suite have no `.config`."""
    cfg = getattr(container, "config", None)
    from core.runtime_paths import data_dir_or_home
    return data_dir_or_home(getattr(cfg, "data_dir", None))


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _default_event_log():
    try:
        from agents.task.telemetry.event_log import get_event_log, event_log_enabled
        if event_log_enabled():
            return get_event_log()
    except Exception:
        pass
    return None


def _record(event_log: Any, user_id: str, session_id: Optional[str], source: str,
            outcome: str, content_hash: str, text: Optional[str] = None,
            attachments: Optional[list] = None) -> None:
    if event_log is None:
        return
    try:
        attrs = {"outcome": outcome, "content_hash": content_hash}
        if text is not None:
            attrs["text"] = str(text)[:500]
        if attachments:
            attrs["attachments"] = [str(e.get("path") or "")
                                    for e in attachments][:5]
        event_log.record(DELIVERY_EVENT_KIND, user_id=str(user_id or ""),
                         session_id=str(session_id or ""), source=source, attrs=attrs)
    except Exception:
        pass


def _record_notice(event_log: Any, user_id: str, text: str) -> None:
    """Durable fallback (extends push_owner_message's owner_notice, T4-04):
    visible via `polyrob telemetry` and rolled into the digest. The notice must
    outlive a disabled telemetry flag — same guarantee the original
    ``_record_owner_notice`` gave — so it falls back to the raw event log."""
    if event_log is None:
        try:
            from agents.task.telemetry.event_log import get_event_log
            event_log = get_event_log()
        except Exception:
            return
    try:
        event_log.record(OWNER_NOTICE, user_id=str(user_id or ""),
                         source="user_delivery", attrs={"text": str(text)[:2000]})
    except Exception:
        pass


def resolve_telegram_recipient(container: Any, user_id: str) -> Optional[str]:
    """Canonical per-tenant telegram recipient resolver (audit T6, 2026-07-16):
    ``user_directory`` service → digit-uid-IS-chat-id (telegram-origin sessions) →
    owner-principal fallback (single-user instances). ``cron/delivery`` delegates
    here — there must be exactly ONE answer to "who is this tenant's chat"."""
    uid = str(user_id or "").strip()
    try:
        directory = container.get_service("user_directory") if container else None
        if directory is not None and uid:
            chat = directory.get_telegram_chat_id(uid)
            if chat:
                return str(chat)
    except Exception:
        pass
    if uid.isdigit():
        return uid
    try:
        import core.instance as _instance
        owner = _instance.resolve_owner_telegram_id()
        return str(owner) if owner else None
    except Exception:
        return None


# Back-compat alias (internal callers/tests predating the T6 rename).
_resolve_recipient = resolve_telegram_recipient


async def deliver_user_message(container: Any, user_id: str, text: str, *,
                               source: str = "agent", session_id: Optional[str] = None,
                               recipient_override: Optional[str] = None,
                               attachments: Optional[list] = None,
                               priority: Optional[str] = None,
                               event_log: Any = ...) -> str:
    """Deliver *text* to *user_id*'s principal through the one rail.

    Returns an outcome string: ``sent`` | ``deduped`` | ``rate_limited`` |
    ``capped`` (suppressed by the daily cap, durably recorded as an
    owner_notice — 019 #2) | ``fallback`` (durably recorded, no live sink /
    send failed) | ``empty``. Never raises.

    ``attachments`` (QW-1, 2026-07-19): pre-validated media entries
    (``core.surfaces.attachments`` shapes — the caller does confinement +
    screening; the rail only transports). Passed to the sink as ``media=``;
    a legacy sink without that kwarg still gets the text (fail-open to
    text-only, never a lost message).

    ``priority`` (2026-07-20): ``critical`` bypasses the daily cap and hourly
    rate limit (dedup still applies) so a halt/security notice can never be
    starved by chatter; ``low`` may only spend ``cap - USER_DELIVERY_RESERVED_SLOTS``;
    ``normal`` (the default for every existing caller) is unchanged. Omitted =>
    derived from *source* (see ``resolve_priority``).
    """
    body = (text or "").strip()
    if not body:
        return "empty"
    if event_log is ...:
        event_log = _default_event_log()
    uid = str(user_id or "")
    h = _content_hash(body)
    now = time.time()

    # --- the rail's memory (fail-open when the event log is unavailable) ----
    try:
        if event_log is not None:
            recent = event_log.query(kind=DELIVERY_EVENT_KIND, user_id=uid,
                                     since_ts=now - _dedup_hours() * 3600, limit=1000)
            consumed = [e for e in recent
                        if (e.get("attrs") or {}).get("outcome") in _CONSUMED_OUTCOMES]
            if any((e.get("attrs") or {}).get("content_hash") == h for e in consumed):
                _record(event_log, uid, session_id, source, "deduped", h)
                return "deduped"
            _home_dir = _home_dir_for_container(container)
            # 018 P0.3 — quiet hours: DEFER, never drop (owner decision
            # 2026-07-18). A durable hold needs the event log, so this branch
            # lives inside the event-log block; without a log we fail open to
            # send. The full body is persisted (held_text) so the release
            # sweep (release_quiet_held, ticker-driven) can deliver it at
            # window-end; quiet_held is NOT a consumed outcome, so dedup
            # ignores it and the release re-entry passes.
            from core.surfaces.quiet_hours import quiet_window_active
            if quiet_window_active(uid, _home_dir):
                try:
                    event_log.record(DELIVERY_EVENT_KIND, user_id=uid,
                                     session_id=str(session_id or ""),
                                     source=source,
                                     attrs={"outcome": "quiet_held",
                                            "content_hash": h,
                                            "held_text": body[:4000]})
                except Exception:
                    logger.debug("user_delivery: quiet hold record failed",
                                 exc_info=True)
                return "quiet_held"
            day = [e for e in consumed if e.get("ts", 0) >= now - 86400]
            lane = resolve_priority(source, priority)
            allowance = effective_cap_for_priority(
                effective_daily_cap(uid, _home_dir), lane)
            if lane != PRIORITY_CRITICAL and len(day) >= allowance:
                # 019 #2: a capped message must not be silently lost — unlike
                # its siblings ("fallback" writes a durable owner_notice,
                # "quiet_held" persists held_text), "capped" used to drop the
                # content irrecoverably (the 2026-07-18 daily digest). Mirror
                # the fallback branch: durable owner_notice (with the source +
                # truncated text so the owner can reconstruct what was
                # suppressed) + the full attempt record carrying the text.
                _record_notice(
                    event_log, uid,
                    f"[suppressed by daily proactive-message cap; "
                    f"source={source}] {body}")
                _record(event_log, uid, session_id, source, "capped", h,
                        text=body, attachments=attachments)
                return "capped"
            hour = [e for e in day if e.get("ts", 0) >= now - 3600]
            if lane != PRIORITY_CRITICAL and \
                    len(hour) >= effective_rate_per_hour(uid, _home_dir):
                _record(event_log, uid, session_id, source, "rate_limited", h)
                return "rate_limited"
    except Exception:
        logger.debug("user_delivery: gate check failed (fail-open)", exc_info=True)

    # --- resolve + send ------------------------------------------------------
    sent = False
    try:
        chat_id = recipient_override or _resolve_recipient(container, uid)
        sink = None
        if container is not None:
            try:
                sink = (container.get_service("telegram_sink")
                        or container.get_service("message_router"))
            except Exception:
                sink = None
        if sink is not None and chat_id:
            if attachments:
                try:
                    res = sink.send_message(str(chat_id), body, media=attachments)
                except TypeError:
                    # pre-QW-1 sink shape (no media kwarg): text still delivers
                    res = sink.send_message(str(chat_id), body)
            else:
                res = sink.send_message(str(chat_id), body)
            if hasattr(res, "__await__"):
                res = await res
            sent = bool(res)
    except Exception as e:
        logger.debug("user_delivery: send failed: %s", e)
        sent = False

    if sent:
        _record(event_log, uid, session_id, source, "sent", h,
                attachments=attachments)
        return "sent"
    # Durable fallback — the message is never silently lost.
    _record_notice(event_log, uid, body)
    _record(event_log, uid, session_id, source, "fallback", h, text=body,
            attachments=attachments)
    return "fallback"


async def release_quiet_held(container: Any, *, event_log: Any = ...,
                             now: Optional[float] = None) -> int:
    """Deliver messages held by the quiet-hours gate whose tenant window has
    ended (018 P0.3). Driven by the autonomy-runtime ticker; also safe to call
    ad hoc. Idempotent by construction: a released message re-enters
    ``deliver_user_message`` and records a CONSUMED outcome (sent/fallback)
    under the same content hash, which both this sweep and the rail's dedup
    skip on the next pass; a ``rate_limited``/``capped`` release attempt stays
    unconsumed and is retried on a later sweep. Returns the delivered count.
    Never raises."""
    if event_log is ...:
        event_log = _default_event_log()
    if event_log is None:
        return 0
    from core.surfaces.quiet_hours import quiet_window_active
    ts_now = now if now is not None else time.time()
    try:
        recent = event_log.query(kind=DELIVERY_EVENT_KIND,
                                 since_ts=ts_now - 48 * 3600, limit=1000)
    except Exception:
        logger.debug("release_quiet_held: query failed", exc_info=True)
        return 0
    consumed = {(str(e.get("user_id") or ""), (e.get("attrs") or {}).get("content_hash"))
                for e in recent
                if (e.get("attrs") or {}).get("outcome") in _CONSUMED_OUTCOMES}
    _home_dir = _home_dir_for_container(container)
    released = 0
    still_quiet: dict = {}
    for e in sorted(recent, key=lambda x: x.get("ts", 0)):  # oldest first
        attrs = e.get("attrs") or {}
        if attrs.get("outcome") != "quiet_held":
            continue
        uid = str(e.get("user_id") or "")
        key = (uid, attrs.get("content_hash"))
        if key in consumed:
            continue
        body = attrs.get("held_text") or ""
        if not body:
            continue
        if uid not in still_quiet:
            still_quiet[uid] = quiet_window_active(uid, _home_dir)
        if still_quiet[uid]:
            continue
        try:
            out = await deliver_user_message(
                container, uid, body,
                source=str(e.get("source") or "quiet_release"),
                session_id=e.get("session_id") or None, event_log=event_log)
        except Exception:
            logger.debug("release_quiet_held: delivery failed", exc_info=True)
            continue
        if out in _CONSUMED_OUTCOMES:
            released += 1
            consumed.add(key)
    return released


async def maybe_deliver_autonomous_send(orchestrator: Any, session_id: str, text: str,
                                        *, event_log: Any = ...) -> Optional[str]:
    """§3.1: route an autonomous session's send_message to its OWN principal.

    Returns None when not routed (interactive session, flag off); otherwise the
    rail outcome. Fail-open: never raises into the send_message action.
    """
    try:
        if not send_message_user_delivery_enabled():
            return None
        from agents.task.goals.autonomy_marker import is_autonomous
        if not is_autonomous(session_id):
            return None
        container = getattr(orchestrator, "container", None)
        user_id = str(getattr(orchestrator, "user_id", "") or "")
        return await deliver_user_message(
            container, user_id, text, source="agent_send", session_id=session_id,
            event_log=event_log)
    except Exception:
        logger.debug("user_delivery: autonomous routing failed (fail-open)",
                     exc_info=True)
        return "failed"
