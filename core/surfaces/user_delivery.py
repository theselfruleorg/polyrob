"""§3.2 — ONE user-bound delivery rail with a memory.

All user-bound sends converge here: the agent's ``send_message`` from
autonomous sessions (§3.1), cron delivery's telegram leg, and the framework
safety-net notices (§3.4). The rail adds what the scattered rails never had:

- **content-hash dedup** (24h window, per tenant) — the watermark-spam class.
  It keys on content the user actually RECEIVED, never on a failed attempt: a
  message nobody saw is not a duplicate of anything;
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
import os
import time
from typing import Any, Optional

from core.env import bool_env, int_env
from core.event_kinds import OWNER_NOTICE, USER_DELIVERY

logger = logging.getLogger(__name__)

DELIVERY_EVENT_KIND = USER_DELIVERY

#: Prefixes stamped on a durable ``owner_notice`` row when the original text
#: could not be delivered live (A7 / A40): suppressed by the daily cap, held
#: by an active owner pause, or undelivered (no live sink / send failed).
#: ``core.surfaces.missed`` matches on every marker so a paused or
#: undelivered notice is as readable as a capped one — before this only the
#: cap marker was matched anywhere, so the other two shapes were silently
#: unreadable via `/missed`.
NOTICE_MARKERS = (
    "[suppressed by daily proactive-message cap",
    "[held by owner pause",
    "[undelivered",
    "[suppressed by hourly rate limit",
    "[suppressed by owner-message cooldown",
)

#: Outcomes that spent a slot of the rate/cap budget. ``fallback`` is included
#: because a LIVE sink was there and the send failed against it: the rail did
#: real work and must not become free retry headroom.
#:
#: ``no_sink`` is deliberately absent (D49, 2026-09-21 interface audit). On a
#: local/REPL/headless owner there is no sink at ALL, so every message takes
#: that branch — charging the daily cap for it would exhaust the budget without
#: one message ever being attempted, and the owner would be capped out of a
#: channel they were never on.
_CONSUMED_OUTCOMES = ("sent", "fallback")

#: Outcomes that prove the text actually REACHED the user. Dedup keys on this,
#: NOT on ``_CONSUMED_OUTCOMES`` (2026-09-15 prod review, C2): matching an
#: ``undelivered`` fallback made a failed send permanent — the retry came back
#: ``deduped`` for 24h, so neither attempt ever reached the owner and the rail
#: reported the loss as a successful duplicate-suppression. A message the owner
#: never saw is not a duplicate of anything.
_DELIVERED_OUTCOMES = ("sent",)

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

#: A lane that is neither gated by the owner budget nor counted against it.
#:
#: D20 (2026-09-21 interface audit): the `message` tool books its owner-tier
#: sends onto this rail's ledger (C5) so the window is honest, but it is gated
#: by its OWN 2h owner-resend cooldown and the outbound policy — not by the
#: daily cap. Recording those rows in the `normal` lane meant an unbounded
#: producer could spend the whole budget that only OTHER producers can be
#: denied for, which is the same shape C1 removed for the critical lane. A lane
#: that cannot be denied must not be able to deny others, so `exempt` skips the
#: gates for itself AND is dropped from the counted window.
PRIORITY_EXEMPT = "exempt"

#: Lanes the daily cap and the hourly rate limit neither gate nor count.
_UNBUDGETED_LANES = (PRIORITY_CRITICAL, PRIORITY_EXEMPT)

#: Sources whose messages are safety-bearing: the owner learning that autonomy
#: STOPPED is never optional, so these bypass the daily cap and the hourly rate
#: limit. They stay subject to content dedup, so a byte-identical re-trip can't
#: become a spam channel (proposal 020 stamps the trip time to keep genuine
#: re-trips distinct). Quiet hours still apply — that gate DEFERS rather than
#: drops, so the notice survives either way.
# Sources whose messages the daily cap may never drop. The test is not "is this
# important?" but "is the agent BLOCKED until the owner reads it?" — a message
# nobody sees is a decision nobody makes, and the run waits forever.
#
# 2026-08-18: the approval + blocked-goal lanes were missing here, and prod paid
# for it. Over 8 days 195 of 196 owner notices were suppressed by the daily cap;
# six of the suppressed were `source=approval` — the owner-queue lane every money
# verb blocks on — while 156 were `self_evolution` goal-started pings. The agent
# then filed `Unblock goal: …` asks that also went unseen, 44 of which were still
# open a month later. Chatter is capped; a question the agent cannot proceed
# without is not.
_CRITICAL_SOURCES = frozenset({
    "credit_sentinel",
    "halt",
    "security",
    "approval",          # tools/controller/approval_queue.py — owner-queue decision
    "payment_approval",  # the same lane for a money SPEND verb
    "goal_blocked",      # agents/task/goals/escalation.py — a stopped goal's need
    "payment_unmatched", # modules/x402/settlement_watcher.py — unexpected on-chain
                         # money the owner must reconcile (rare, always owner-actionable)
    # modules/x402/x402_integration.py::mark_payment_refund_due — a machine
    # payment that SETTLED and whose work then failed downstream. The agent owes
    # a stranger money back, and the obligation only grows while nobody acts on
    # it, so it belongs beside `payment_unmatched` rather than in a bucket the
    # daily cap can drop. Rare by construction, and always owner-actionable.
    "payment_refund_due",
    # 035 P0-2: core/self_evolution.py::NOTIFY_SOURCE — "I've proposed N change(s)
    # to how I work, approve to make them stick". This used to ride the default
    # ``self_evolution`` source, i.e. the LIFECYCLE bucket below, shared with
    # "▶ goal started" chatter. On 2026-09-08 seven lifecycle pings exhausted that
    # bucket at 13:20 and the approval prompt was dropped (`capped`) at 14:01 —
    # after which a content-independent fingerprint made the loss permanent and
    # four owner directives sat inert for two days. An owner DECISION the agent is
    # blocked on belongs with `approval`/`goal_blocked`, not with chatter.
    "pending_approval",
    # 039 Unit A: core/wallet/tx_notify.py — "this transaction broadcast" and
    # "this is what it did". Money that has ALREADY MOVED may not queue behind
    # "▶ goal started": over 8 days in August the shared cap dropped 195 of 196
    # owner notices. The test is the same one the comment above states — the
    # owner is blocked until they read this, because until they do they do not
    # know where their funds are.
    "tx_execution",
})


def _reserved_slots() -> int:
    """Slots of the daily cap that ``priority="low"`` traffic may not consume."""
    return int_env("USER_DELIVERY_RESERVED_SLOTS", 8)


#: Framework lifecycle chatter — "▶ goal started", "✅ Background goal … completed",
#: "▶ cron run started" — all ride ``source="self_evolution"``. It shares the
#: owner's daily cap with the agent's OWN reports, and with ~35 goal runs/day it
#: alone produces ~70 pings/day against a 30-slot cap: prod 2026-08-24..28 sent
#: exactly 30/30/30/23/25 a day, 200 start pings + 58 completion pings were
#: capped, and on 08-27 only 3 of the agent's 171 attempted messages reached the
#: owner. The lifecycle bucket below is a SEPARATE, smaller ceiling for this
#: source, so it can never crowd the agent's voice out of the shared cap.
_LIFECYCLE_SOURCES = frozenset({"self_evolution", "lifecycle"})

#: Sources whose suppressed text does NOT enter the owner's `/missed` store.
#:
#: Deliberately NARROWER than ``_LIFECYCLE_SOURCES`` (2026-09-15 review, C3).
#: ``self_evolution`` is ``push_owner_message``'s DEFAULT source, so it carries
#: real content too ("I'm blocked; grant twitter access") as well as chatter —
#: keying the notice on it would silence the escalations `/missed` exists for.
#: Only the run-lifecycle pings, which now pass ``source="lifecycle"``
#: explicitly, are excluded: they were 822 of the 899 rows there, so four of
#: the five entries `/missed` renders were "▶ goal started". A goal COMPLETION
#: is not in here — that one carries the result.
_NO_NOTICE_SOURCES = frozenset({"lifecycle"})
#: 031: sources the owner pause holds AT THE RAIL (one choke point, not N call
#: sites) -> the autonomy_control kind that decides it. Critical sources
#: (crash/security/credit) are never listed here.
_PAUSE_KIND_BY_SOURCE = {"self_evolution": "lifecycle_ping", "lifecycle": "lifecycle_ping",
                         "goal_blocked": "escalate"}


def _lifecycle_daily_cap() -> int:
    """Max ``self_evolution`` (lifecycle ping) sends per tenant per rolling 24h.
    ``0`` disables the bucket (lifecycle traffic then only obeys the shared cap)."""
    return int_env("USER_DELIVERY_LIFECYCLE_DAILY_CAP", 10)


def resolve_priority(source: str, priority: Optional[str]) -> str:
    """Explicit *priority* wins; otherwise derive it from *source*.

    Deriving from source means the credit sentinel needed no call-site change —
    it already sends with ``source="credit_sentinel"``.
    """
    if priority in (PRIORITY_CRITICAL, PRIORITY_NORMAL, PRIORITY_LOW,
                    PRIORITY_EXEMPT):
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


def _operator_int(name: str) -> Optional[int]:
    """The operator's EXPLICIT value for *name*, or None when they set nothing.

    2026-09-15 prod review, C12. ``delivery.daily_cap`` / ``delivery.rate_per_hour``
    merge ``min(pref, env_value)``, and these accessors passed the result of
    ``int_env(...)`` — which returns the FRAMEWORK DEFAULT (30 / 10) even when the
    variable is unset, as it is on prod. So the min-merge always clamped against
    the default and the owner could not RAISE their own budget from any seat:
    `/config set delivery.daily_cap 60` resolved back to 30, while the health item
    for a capped message advises exactly that command.

    The min-merge is correct when the operator set a real ceiling — a pref must
    not widen past what the deployment allows. With no operator value there is no
    ceiling to widen past, which is the same reasoning ``narrow_list`` already
    states for an empty operator set. Returning None here is what tells
    ``prefs.resolve`` "no operator opinion", so the pref wins.
    """
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def effective_rate_per_hour(user_id: Optional[str], home_dir) -> int:
    """Owner's proactive-message rate limit: pref (spec ``delivery.rate_per_hour``,
    min-merged against an EXPLICIT ``USER_DELIVERY_RATE_PER_HOUR`` only) over the
    default. No pref file present => byte-identical to ``_rate_per_hour()``."""
    from core import prefs
    return prefs.resolve("delivery.rate_per_hour", user_id, home_dir,
                         env_value=_operator_int("USER_DELIVERY_RATE_PER_HOUR"),
                         default=_rate_per_hour())


def effective_daily_cap(user_id: Optional[str], home_dir) -> int:
    """Owner's proactive-message daily cap: pref (spec ``delivery.daily_cap``,
    min-merged against an EXPLICIT ``USER_DELIVERY_DAILY_CAP`` only) over the
    default. No pref file present => byte-identical to ``_daily_cap()``."""
    from core import prefs
    return prefs.resolve("delivery.daily_cap", user_id, home_dir,
                         env_value=_operator_int("USER_DELIVERY_DAILY_CAP"),
                         default=_daily_cap())


def _home_dir_for_container(container: Any) -> str:
    """Home for pref resolution (``delivery.daily_cap``/``rate_per_hour``, quiet
    hours) — the IDENTITY axis, which is the data home and nothing else.

    ⚠️ This used to read the container's ``config.data_dir``, which on a server
    is ``<data_home>/data`` (a shadow one level down, see
    ``core.runtime_paths.prefs_home_dir``) while every preference WRITER
    resolves the data home. The owner could raise the cap on any seat and this
    rail would keep reading the env default. *container* is kept in the
    signature because callers pass it and a future per-tenant home may need it.
    """
    from core.runtime_paths import prefs_home_dir
    return prefs_home_dir()


def content_hash(text: str) -> str:
    """The rail's content fingerprint — the ONE definition of "the same message".

    Public because two gates outside this module compare against it (the
    `message` tool's owner-resend cooldown and its owner-send bookkeeping); a
    second hash would be a second opinion on what counts as a repeat.
    """
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


#: Back-compat alias for the in-module call sites and existing tests.
_content_hash = content_hash


def _default_event_log():
    try:
        from core.event_log import get_event_log, event_log_enabled
        if event_log_enabled():
            return get_event_log()
    except Exception:
        pass
    return None


def _record(event_log: Any, user_id: str, session_id: Optional[str], source: str,
            outcome: str, content_hash: str, text: Optional[str] = None,
            attachments: Optional[list] = None,
            lane: Optional[str] = None, extra: Optional[dict] = None) -> None:
    if event_log is None:
        return
    try:
        attrs = {"outcome": outcome, "content_hash": content_hash,
                 "lane": lane or resolve_priority(source, None)}
        if extra:
            attrs.update(extra)
        if text is not None:
            attrs["text"] = str(text)[:500]
        if attachments:
            attrs["attachments"] = [str(e.get("path") or "")
                                    for e in attachments][:5]
        event_log.record(DELIVERY_EVENT_KIND, user_id=str(user_id or ""),
                         session_id=str(session_id or ""), source=source, attrs=attrs)
    except Exception:
        # D47: this row IS the rail's audit trail and its cap memory. Losing it
        # silently is how a gate stops gating and no seat can say why.
        logger.warning("user_delivery: attempt record failed (outcome=%s source=%s) "
                       "— this send is missing from the rail's ledger",
                       outcome, source, exc_info=True)


def _event_lane(event: dict) -> str:
    """The priority lane a recorded attempt rode.

    Prefers the lane stamped at record time; falls back to deriving it from the
    source so rows written before the stamp existed still classify correctly.
    """
    attrs = event.get("attrs") or {}
    lane = attrs.get("lane")
    if lane in (PRIORITY_CRITICAL, PRIORITY_NORMAL, PRIORITY_LOW, PRIORITY_EXEMPT):
        return str(lane)
    return resolve_priority(str(event.get("source") or ""), None)


def _budgeted(events: list) -> list:
    """The subset of *events* that may be counted against the shared daily cap
    and the hourly rate limit.

    2026-09-15 prod review, C1: ``_CRITICAL_SOURCES`` skip both gates for
    themselves but used to land in the counted window anyway, so every
    cap-exempt send silently spent a slot only NON-exempt traffic could be
    denied for. Measured over 2026-09-08..15: 105 of 154 delivered messages
    were critical-lane, and on 2026-09-14 sixteen `tx_execution` notices plus
    nine approval prompts consumed the owner's whole 30-slot budget — the
    agent's own voice got one send that day and was capped fifteen times that
    week. A lane that cannot be denied must not be able to deny others.

    D20 extends the same rule to the ``exempt`` lane (the `message` tool's own
    bookkeeping rows), for the same reason.
    """
    return [e for e in events if _event_lane(e) not in _UNBUDGETED_LANES]


def _count(event_log: Any, *, since_ts: float, user_id: str,
           attrs_in: Optional[dict] = None, attrs_not_in: Optional[dict] = None,
           sources_in=None, sources_not_in=None) -> Optional[int]:
    """Count matching ``user_delivery`` rows — in SQL when the store can (D48).

    A store without ``count_where`` (an in-memory test double) is counted in
    Python over its ``query`` window, which is what every caller used to do.
    Returns None when the count could not be taken at all, so the caller fails
    OPEN (send) rather than gating on a number it does not have.
    """
    counter = getattr(event_log, "count_where", None)
    if callable(counter):
        try:
            return counter(kind=DELIVERY_EVENT_KIND, user_id=user_id,
                           since_ts=since_ts, attrs_in=attrs_in,
                           attrs_not_in=attrs_not_in, sources_in=sources_in,
                           sources_not_in=sources_not_in)
        except Exception:
            logger.warning("user_delivery: count_where failed, counting in memory",
                           exc_info=True)
    try:
        rows = event_log.query(kind=DELIVERY_EVENT_KIND, user_id=user_id,
                               since_ts=since_ts, limit=1000)
    except Exception:
        logger.warning("user_delivery: window query failed (fail-open)", exc_info=True)
        return None
    n = 0
    for e in rows:
        attrs = e.get("attrs") or {}
        src = str(e.get("source") or "")
        if sources_in is not None and src not in sources_in:
            continue
        if sources_not_in is not None and src in sources_not_in:
            continue
        ok = True
        for name, values in (attrs_in or {}).items():
            if attrs.get(name) not in values:
                ok = False
                break
        for name, values in (attrs_not_in or {}).items():
            value = attrs.get(name)
            if value is not None and value in values:
                ok = False
                break
        if ok:
            n += 1
    return n


def _budget_count_filters() -> dict:
    """``EventLog.count_where`` kwargs selecting exactly what :func:`_budgeted`
    keeps — the SQL twin of that predicate, so the two can never disagree about
    what the daily cap counts.

    The lane is stamped on every row this module writes; ``sources_not_in``
    covers the legacy rows written before the stamp existed, which
    :func:`_event_lane` classifies from the source alone.
    """
    return {"attrs_not_in": {"lane": _UNBUDGETED_LANES},
            "sources_not_in": tuple(sorted(_CRITICAL_SOURCES))}


#: Outcomes that already wrote a marked ``owner_notice`` for their body. A
#: SECOND suppression of the same body must not write a second notice —
#: ``/missed`` shows five entries, and filling them with one repeated body is
#: the failure C3 just removed. ``sent``/``deduped``/``quiet_held`` are absent
#: on purpose: none of them wrote a notice, so none of them may suppress one.
_NOTICE_OUTCOMES = ("capped", "paused", "fallback", "rate_limited", "cooldown",
                    "no_sink")


def _notice_already_written(event_log: Any, user_id: str, content_hash: str,
                            now: float) -> bool:
    """True when this exact body already has an ``owner_notice`` in the window.

    Round-2 review: C9 gave this rule to the ``paused`` branch alone, and its
    four siblings did not get it — while C2 made ``fallback`` retryable, so a
    dead sink re-wrote its notice on every attempt. The ATTEMPT row is still
    written every time; only the recovery entry is deduplicated.

    Fail-open to False: a query fault costs a duplicate notice, never a lost one.

    D48: counted in SQL. The previous ``limit=1000`` window silently truncated
    on a busy tenant, so the check stopped working exactly when the noise it
    guards against was worst.
    """
    n = _count(event_log, user_id=str(user_id or ""),
               since_ts=now - _dedup_hours() * 3600,
               attrs_in={"content_hash": (content_hash,),
                         "outcome": _NOTICE_OUTCOMES})
    return bool(n)


def _maybe_notice(event_log: Any, user_id: str, source: str, text: str, *,
                  content_hash: Optional[str] = None,
                  now: Optional[float] = None) -> None:
    """Write an owner_notice unless *source* is framework lifecycle chatter, or
    this exact body already has one in the window.

    Both rules live HERE rather than at the five call sites, because the
    round-2 review found exactly the "applied to one branch of a set" mistake
    that pattern invites.

    2026-09-15 prod review, C3: 822 of the 899 rows in the owner's ``/missed``
    store were run-lifecycle pings, so four of the five entries ``/missed``
    renders were ``▶ goal started`` and the one real report was buried.
    ``/missed`` is the owner's RECOVERY channel — it holds content they still
    need. A start ping is ephemeral status: the ``user_delivery`` attempt row
    (which carries the full text) remains the durable audit record, so nothing
    is silently dropped.
    """
    if str(source or "") in _NO_NOTICE_SOURCES:
        return
    if content_hash and _notice_already_written(
            event_log, user_id, content_hash, now if now is not None else time.time()):
        return
    _record_notice(event_log, user_id, text, content_hash=content_hash)


def _record_notice(event_log: Any, user_id: str, text: str, *,
                   content_hash: Optional[str] = None) -> None:
    """Durable fallback (extends push_owner_message's owner_notice, T4-04):
    visible via `polyrob telemetry` and rolled into the digest. The notice must
    outlive a disabled telemetry flag — same guarantee the original
    ``_record_owner_notice`` gave — so it falls back to the raw event log.

    ``content_hash`` is stamped on the row (D50) so ``core.surfaces.missed`` can
    tell a notice whose body was LATER delivered from one the owner still has
    not seen, without re-hashing a truncated copy of the text.
    """
    if event_log is None:
        try:
            from core.event_log import get_event_log
            event_log = get_event_log()
        except Exception:
            logger.warning("user_delivery: no event log for the owner notice — "
                           "this undelivered message is not recoverable via /missed",
                           exc_info=True)
            return
    attrs = {"text": str(text)[:2000]}
    if content_hash:
        attrs["content_hash"] = str(content_hash)
    try:
        event_log.record(OWNER_NOTICE, user_id=str(user_id or ""),
                         source="user_delivery", attrs=attrs)
    except Exception:
        # D47: the notice IS the recovery channel for a message the owner never
        # received. Losing it silently loses the message twice.
        logger.warning("user_delivery: owner notice record failed — an undelivered "
                       "message will not appear in /missed", exc_info=True)


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


#: Subject line for an owner notice delivered over email, by producer source.
#: D70: every notice used to arrive as "Re: your message" — a reply header on
#: something that answers nothing, so the owner's mail client threaded a cron
#: report under an unrelated conversation and the subject said nothing about
#: what had happened.
_NOTICE_SUBJECT_BY_SOURCE = {
    "cron": "scheduled run result",
    "digest": "daily digest",
    "approval": "approval needed",
    "payment_approval": "payment approval needed",
    "pending_approval": "changes waiting for your approval",
    "goal_blocked": "a goal is blocked",
    "credit_sentinel": "autonomy stopped (credits)",
    "halt": "autonomy halted",
    "security": "security notice",
    "tx_execution": "transaction notice",
    "payment_unmatched": "unmatched payment",
    "payment_refund_due": "refund owed on a settled payment",
    "agent_send": "message from your agent",
    "self_evolution": "agent update",
    "lifecycle": "run status",
}


def notice_subject(source: str) -> str:
    """The email subject for a rail notice from *source*.

    An unknown source gets the generic-but-honest form rather than a "Re:"
    that claims to answer something.
    """
    label = _NOTICE_SUBJECT_BY_SOURCE.get(str(source or ""))
    return f"[POLYROB] {label}" if label else f"[POLYROB] {source or 'notice'}"


async def deliver_user_message(container: Any, user_id: str, text: str, *,
                               source: str = "agent", session_id: Optional[str] = None,
                               recipient_override: Optional[str] = None,
                               recipient_surface: Optional[str] = None,
                               attachments: Optional[list] = None,
                               priority: Optional[str] = None,
                               event_log: Any = ...) -> str:
    """Deliver *text* to *user_id*'s principal through the one rail.

    Returns an outcome string: ``sent`` | ``deduped`` | ``rate_limited`` |
    ``capped`` (suppressed by the daily cap, durably recorded as an
    owner_notice — 019 #2) | ``queued`` (the body was handed to the durable
    cross-process queue; delivery is still OWED, so it is never ``sent``, but
    it is not a fault either) | ``fallback`` (a live sink was there and the
    send FAILED — durably recorded) | ``no_sink`` (no sink existed at all: the
    durable owner_notice IS the channel on a local/REPL/headless owner, so it
    spends no budget — D21/D49) | ``empty``. Never raises.

    ⚠️ ``queued`` and ``fallback`` share ONE recorded row outcome
    (``fallback``), because the budget question — did the rail do real work? —
    has the same answer for both. They differ only in what the CALLER is told.

    ``recipient_surface`` (D44) names the surface ``recipient_override``
    addresses. It defaults to ``telegram``, which is what every pre-existing
    caller meant, so an omitted value is byte-identical.

    EVERY outcome now records the body on its attempt row, so "what did it try
    to tell me" and "what did it actually tell me" are both answerable; and
    every outcome the owner did not receive — except lifecycle chatter — also
    writes the marked ``owner_notice`` that ``/missed`` renders.

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
    # 2026-09-15: the SECOND owner rail gets the same remedy check the `message`
    # tool now gets. Everything the framework itself pushes at the owner — a
    # goal escalation, a pending-approval notice, a settlement notice — arrives
    # here, and every one of them reaches a reader with no shell. Content-only
    # (it appends, never suppresses), applied BEFORE the dedup hash so a
    # corrected body and its uncorrected twin are one message, not two.
    try:
        from core.owner_remedy import correction_line, shell_free_correction
        _fixups = correction_line(body) + shell_free_correction(body)
        if _fixups:
            logger.info("owner delivery carried unreachable actions; corrected inline")
            body = body + _fixups
    except Exception:
        logger.debug("owner remedy check skipped", exc_info=True)
    if event_log is ...:
        event_log = _default_event_log()
    uid = str(user_id or "")
    h = _content_hash(body)
    now = time.time()
    # Round-2 review: the EFFECTIVE lane, resolved once. `_record` used to
    # re-derive it from the source alone, ignoring the explicit `priority=`
    # the gates honour one line below — so a caller passing
    # priority="critical" skipped the cap for itself and then spent a slot
    # anyway, which is the very bug C1 exists to remove.
    lane = resolve_priority(source, priority)

    # 031 owner pause: a lifecycle ping / escalation is held, durably recorded as
    # an owner_notice (visible in /missed + the digest), never silently dropped.
    _pk = _PAUSE_KIND_BY_SOURCE.get(str(source or ""))
    if _pk is not None:
        from core.autonomy_control import allows as _allows
        _dec = _allows(_pk)
        if not _dec.allowed:
            if event_log is not None:
                # 2026-09-15 prod review, C9: ``paused`` is not a consumed
                # outcome, so the producer re-offers the same body on every
                # tick. One notice per held body is a recovery entry; one per
                # tick is the noise that buried the real ones — prod held the
                # identical "I've proposed 3 change(s)" text seven times. The
                # ATTEMPT is still recorded every time (that is the audit).
                _maybe_notice(event_log, uid, source,
                              f"[held by owner pause; source={source}] {body}",
                              content_hash=h, now=now)
                _record(event_log, uid, session_id, source, "paused", h, text=body,
                        attachments=attachments, lane=lane)
            return "paused"

    # --- the rail's memory (fail-open when the event log is unavailable) ----
    try:
        if event_log is not None:
            # D48: every window below is a COUNT, not a 1000-row fetch — the old
            # window truncated on a busy tenant, so the gates under-counted
            # exactly when traffic was highest.
            _window = now - _dedup_hours() * 3600
            if _count(event_log, user_id=uid, since_ts=_window,
                      attrs_in={"content_hash": (h,),
                                "outcome": _DELIVERED_OUTCOMES}):
                # The owner HAS this text, so no notice — but the attempt row
                # carries the body (C2): 383 of 383 deduped rows in prod held a
                # NULL text, which made "what did it try to tell me" unanswerable.
                _record(event_log, uid, session_id, source, "deduped", h, text=body,
                        attachments=attachments, lane=lane)
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
                                            "content_hash": h, "lane": lane,
                                            "held_text": body[:4000]})
                except Exception:
                    logger.debug("user_delivery: quiet hold record failed",
                                 exc_info=True)
                return "quiet_held"
            # C1/D20: only traffic the cap can DENY is counted against it.
            _budget = dict(_budget_count_filters())
            _budget["attrs_in"] = {"outcome": _CONSUMED_OUTCOMES}
            day = _count(event_log, user_id=uid, since_ts=now - 86400, **_budget)
            if day is None:
                day = 0  # fail-open: an uncountable window never denies a send
            allowance = effective_cap_for_priority(
                effective_daily_cap(uid, _home_dir), lane)
            _lc_cap = _lifecycle_daily_cap()
            if lane not in _UNBUDGETED_LANES and source in _LIFECYCLE_SOURCES and _lc_cap > 0:
                lifecycle_day = _count(
                    event_log, user_id=uid, since_ts=now - 86400,
                    sources_in=tuple(sorted(_LIFECYCLE_SOURCES)),
                    attrs_in={"outcome": _CONSUMED_OUTCOMES},
                    attrs_not_in=_budget["attrs_not_in"]) or 0
                if lifecycle_day >= _lc_cap:
                    _maybe_notice(
                        event_log, uid, source,
                        f"[suppressed by daily proactive-message cap; "
                        f"source={source}; bucket=lifecycle] {body}",
                        content_hash=h, now=now)
                    _record(event_log, uid, session_id, source, "capped", h,
                            text=body, attachments=attachments, lane=lane)
                    return "capped"
            if lane not in _UNBUDGETED_LANES and day >= allowance:
                # 019 #2: a capped message must not be silently lost — unlike
                # its siblings ("fallback" writes a durable owner_notice,
                # "quiet_held" persists held_text), "capped" used to drop the
                # content irrecoverably (the 2026-07-18 daily digest). Mirror
                # the fallback branch: durable owner_notice (with the source +
                # truncated text so the owner can reconstruct what was
                # suppressed) + the full attempt record carrying the text.
                _maybe_notice(
                    event_log, uid, source,
                    f"[suppressed by daily proactive-message cap; "
                    f"source={source}] {body}", content_hash=h, now=now)
                _record(event_log, uid, session_id, source, "capped", h,
                        text=body, attachments=attachments, lane=lane)
                return "capped"
            hour = _count(event_log, user_id=uid, since_ts=now - 3600, **_budget) or 0
            if lane not in _UNBUDGETED_LANES and \
                    hour >= effective_rate_per_hour(uid, _home_dir):
                # C2: the owner did NOT receive this. Its sibling `capped`
                # has written a marked owner_notice since 019 #2; this branch
                # never did, so 18 rate-limited messages were readable only by
                # someone querying telemetry by hand.
                _maybe_notice(
                    event_log, uid, source,
                    f"[suppressed by hourly rate limit; source={source}] {body}",
                    content_hash=h, now=now)
                _record(event_log, uid, session_id, source, "rate_limited", h,
                        text=body, attachments=attachments, lane=lane)
                return "rate_limited"
    except Exception:
        logger.debug("user_delivery: gate check failed (fail-open)", exc_info=True)

    # --- long-body spill (G8) ------------------------------------------------
    # A chat message is not a document. Over the threshold the body becomes an
    # attached report and the owner reads a gist. Applied AFTER the gates so
    # dedup/rate/cap still key on the ORIGINAL content, and the durable fallback
    # below still records the full body — a spill must never lose content.
    send_body = body
    send_attachments = list(attachments or [])
    try:
        from core.surfaces.spill import maybe_spill
        spilled = maybe_spill(body, home_dir=_home_dir_for_container(container),
                              source=source)
        if spilled is not None:
            send_body, entry = spilled
            send_attachments.append(entry)
    except Exception:
        logger.debug("user_delivery: spill skipped (fail-open)", exc_info=True)
        send_body, send_attachments = body, list(attachments or [])

    # --- resolve + send (030 WS-B1: surface-aware owner fan-out) -------------
    # Legacy (OWNER_SURFACE unset) is byte-compatible: one telegram target,
    # telegram_sink preferred. With OWNER_SURFACE set, the chain is primary +
    # fallback; a CRITICAL-lane notice broadcasts to every configured surface.
    # D6/D21/D49: three distinct answers, not two. ``sent`` = a live sink took
    # it; ``queued`` = the durable cross-process queue took it (delivery is
    # still owed, so it may never be recorded as ``sent``); ``no_sink`` = there
    # was no sink to try at all.
    best = "no_sink"
    try:
        from core.surfaces.owner_address import owner_address, owner_surface_order
        if recipient_override:
            targets = [(str(recipient_surface or "telegram"), str(recipient_override))]
        else:
            targets = []
            for _sid in owner_surface_order():
                _addr = owner_address(container, _sid, uid)
                if _addr:
                    targets.append((_sid, str(_addr)))
        tg_sink = router = None
        if container is not None:
            try:
                tg_sink = container.get_service("telegram_sink")
                router = container.get_service("message_router")
            except Exception:
                tg_sink = router = None

        async def _send_one(_sid: str, _addr: str) -> str:
            """``"sent"`` | ``"queued"`` | ``"failed"`` | ``"no_sink"``."""
            sink = tg_sink if (_sid == "telegram" and tg_sink is not None) else router
            if sink is None:
                return "no_sink"
            kwargs = {} if _sid == "telegram" and sink is tg_sink else {"surface_id": _sid}
            # D70: an email notice is not a reply, so it must not arrive with a
            # "Re:" subject. The subject rides as the legacy media entry every
            # other surface ignores.
            if _sid == "email":
                kwargs["subject"] = notice_subject(source)
            # A router that can tell "sent" from "queued" is asked for the
            # distinction; any other sink keeps the boolean contract.
            _ex = getattr(sink, "send_message_ex", None)
            send = _ex if callable(_ex) else sink.send_message

            def _attempt(text_):
                """Call the sink, shedding ONE optional kwarg at a time.

                Order matters: ``subject`` is a nicety, ``surface_id`` is the
                ROUTING. Shedding both at once — which the pre-existing
                two-step fallback did — delivers a message addressed to email
                over the default surface instead.
                """
                for drop in ((), ("subject",), ("subject", "surface_id")):
                    kw = {k: v for k, v in kwargs.items() if k not in drop}
                    try:
                        return send(_addr, text_, **kw)
                    except TypeError:
                        continue
                return sink.send_message(_addr, text_)

            if send_attachments:
                try:
                    res = send(_addr, send_body, media=send_attachments, **kwargs)
                except TypeError:
                    # pre-QW-1 sink shape (no media kwarg): the attachment cannot
                    # ride, so send the FULL body — a gist pointing at a file the
                    # owner will never receive is worse than a long message.
                    res = _attempt(body)
            else:
                res = _attempt(body)
            if hasattr(res, "__await__"):
                res = await res
            if isinstance(res, str):
                return res if res in ("sent", "queued", "failed") else "failed"
            return "sent" if res else "failed"

        #: better-to-worse; the rail reports the BEST answer any target gave.
        _RANK = {"sent": 3, "queued": 2, "failed": 1, "no_sink": 0}
        broadcast = lane == PRIORITY_CRITICAL and len(targets) > 1
        for _sid, _addr in targets:
            outcome = await _send_one(_sid, _addr)
            if _RANK[outcome] > _RANK[best]:
                best = outcome
            if best == "sent" and not broadcast:
                break
    except Exception as e:
        # D47: a raise here means NOTHING was delivered and the rail is about to
        # fall back. A debug line made that invisible in the journal.
        logger.warning("user_delivery: send path raised (%s) — falling back to a "
                       "durable notice", e, exc_info=True)
        best = "failed"

    if best == "sent":
        # C7: all 527 `sent` rows in prod held a NULL text, so no surface could
        # answer "what did you actually tell me" — only failures were legible.
        _record(event_log, uid, session_id, source, "sent", h, text=body,
                attachments=send_attachments, lane=lane)
        return "sent"
    if best == "queued":
        # D6: durable acceptance is NOT delivery. Recording it as `sent` made
        # the 24h dedup refuse the retry after the queued copy dead-lettered,
        # so neither attempt ever reached the owner. No owner_notice: the body
        # is on its way and a /missed entry for it would be noise.
        #
        # The ROW stays `fallback` — the rail did real work and must spend a
        # budget slot for it (`_CONSUMED_OUTCOMES`) — but the RETURN is
        # `queued`, because the caller's two questions are different ones. A
        # cron report handed to the cross-process queue used to come back as
        # `fallback` and be journalled `failed`, which is the "a working rail
        # reads as a broken one" class D45 set out to end; nothing else in the
        # tree keys on the literal.
        _record(event_log, uid, session_id, source, "fallback", h, text=body,
                attachments=attachments, lane=lane, extra={"queued": True})
        return "queued"
    # Durable fallback — the message is never silently lost. Marker-prefixed
    # (A7 / A40) like the cap/pause notices, so `/missed` can read it too —
    # before this it carried NO marker at all and was unreadable there.
    _maybe_notice(event_log, uid, source, f"[undelivered; source={source}] {body}",
                  content_hash=h, now=now)
    # D21/D49: `no_sink` is its own outcome. "There was no channel to try" and
    # "the channel was there and refused" are different facts, and only the
    # second is evidence of a delivery FAULT. `no_sink` spends no budget (see
    # _CONSUMED_OUTCOMES) because on a local owner it is every message.
    outcome = "no_sink" if best == "no_sink" else "fallback"
    _record(event_log, uid, session_id, source, outcome, h, text=body,
            attachments=attachments, lane=lane)
    return outcome


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
    """§3.1: route a send_message to its session's OWN principal when nothing
    else will.

    Two cases reach this rail: a goal/cron-spawned AUTONOMOUS session (the
    original case — no interactive surface exists at all), and an interactive
    session whose orchestrator has no LIVE chat surface bound in THIS process
    (``_message_router``/``_chat_session_key`` unset). The second case covers
    ``polyrob run --resume`` and any other recreation path that rebuilds the
    orchestrator without rebinding the chat mirror (``_rebind_recreated_chat``
    is best-effort and the in-process ``is_autonomous`` marker never survives
    a process boundary, so a resumed chat session looked "interactive" here
    and its reply was silently dropped — confirmed live 2026-08-28, the reply
    never reached the owner despite send_message reporting success).

    Returns None only when a live mirror IS bound (it already handles
    delivery — routing here too would risk a double-send); otherwise the rail
    outcome. Fail-open: never raises into the send_message action.
    """
    try:
        if not send_message_user_delivery_enabled():
            return None
        # 044 T20 fix round 2 (Obs 1): a PUBLIC (room-bound) session never mirrors
        # to the owner's DM — autonomous or not. The autonomous carve-out below
        # exists because a goal/cron run has no surface of its own; a room SERVICE
        # run has one, and is autonomous, so every public answer was ALSO pushed
        # into the owner's private chat and spent his daily delivery budget on a
        # message he did not ask for and had already read in the room.
        from core.surfaces.room_policy import is_public_session
        if is_public_session(orchestrator):
            return None
        from agents.task.goals.autonomy_marker import is_autonomous
        from core.surfaces.binding import terminal_attached
        has_live_mirror = bool(
            getattr(orchestrator, "_message_router", None)
            and getattr(orchestrator, "_chat_session_key", None)
        )
        # A foreground terminal (REPL / one-shot run) renders the reply from the
        # feed itself; it is a live surface even though it binds no router.
        if (has_live_mirror or terminal_attached(orchestrator)) \
                and not is_autonomous(session_id):
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
