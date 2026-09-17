"""§3.2 — ONE user-bound delivery rail with a memory.

All user-bound sends (agent send_message from autonomous sessions, cron
delivery, framework safety-net notices) pass through one function with:
content-hash dedup (24h), per-tenant rate limit + daily cap, and a durable
owner_notice fallback when no live sink exists. Recipient is resolved
per-tenant (user_directory → digit-uid-is-chat-id → owner-principal fallback);
a session may message its OWN principal only.
"""
import asyncio
import time

import pytest


class _EvLog:
    """In-memory stand-in for TelemetryEventLog (record/query subset)."""

    def __init__(self):
        self.events = []

    def record(self, kind, *, user_id="", session_id="", source="", ts=None,
               attrs=None, **kw):
        merged = dict(kw)
        if attrs:
            merged.update(attrs)
        self.events.append({"ts": ts if ts is not None else time.time(), "kind": kind,
                            "user_id": user_id, "session_id": session_id,
                            "source": source, "attrs": merged})

    def query(self, *, since_ts=None, kind=None, user_id=None, limit=500):
        out = [e for e in self.events
               if (kind is None or e["kind"] == kind)
               and (user_id is None or e["user_id"] == user_id)
               and (since_ts is None or e["ts"] >= since_ts)]
        return sorted(out, key=lambda e: -e["ts"])[:limit]


class _Sink:
    def __init__(self, ok=True):
        self.sent = []
        self._ok = ok

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        return self._ok


class _Directory:
    def __init__(self, mapping):
        self._m = mapping

    def get_telegram_chat_id(self, user_id):
        return self._m.get(user_id)


class _Container:
    def __init__(self, services):
        self._s = services

    def get_service(self, name):
        return self._s.get(name)


def _deliver(container, user_id, text, **kw):
    from core.surfaces.user_delivery import deliver_user_message
    return asyncio.run(deliver_user_message(container, user_id, text, **kw))


def test_sends_to_digit_uid_as_chat_id():
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    out = _deliver(c, "12345", "progress: started the task", event_log=ev)
    assert out == "sent"
    assert sink.sent == [("12345", "progress: started the task")]
    assert any(e["kind"] == "user_delivery" and e["attrs"].get("outcome") == "sent"
               for e in ev.events)


def test_prefers_user_directory_resolution():
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink,
                    "user_directory": _Directory({"alice": "777"})})
    assert _deliver(c, "alice", "hello", event_log=ev) == "sent"
    assert sink.sent[0][0] == "777"


def test_owner_fallback_for_non_numeric_tenant(monkeypatch):
    monkeypatch.setattr("core.instance.resolve_owner_telegram_id", lambda *a, **k: "555")
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    assert _deliver(c, "rob", "blocker: x402 store unavailable", event_log=ev) == "sent"
    assert sink.sent[0][0] == "555"


def test_dedup_suppresses_identical_content_within_window():
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    assert _deliver(c, "1", "same text", event_log=ev) == "sent"
    assert _deliver(c, "1", "same text", event_log=ev) == "deduped"
    assert len(sink.sent) == 1
    assert _deliver(c, "1", "different text", event_log=ev) == "sent"


def test_dedup_is_per_tenant():
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    assert _deliver(c, "1", "same text", event_log=ev) == "sent"
    assert _deliver(c, "2", "same text", event_log=ev) == "sent"


def test_rate_limit_per_hour(monkeypatch):
    monkeypatch.setenv("USER_DELIVERY_RATE_PER_HOUR", "2")
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    assert _deliver(c, "1", "msg one", event_log=ev) == "sent"
    assert _deliver(c, "1", "msg two", event_log=ev) == "sent"
    assert _deliver(c, "1", "msg three", event_log=ev) == "rate_limited"
    assert len(sink.sent) == 2


def test_rate_limited_records_text(monkeypatch):
    """A7 / A40: the 11th message in an hour used to vanish entirely — the
    `user_delivery` row recorded no text at all. It must now carry the body
    (mirroring the `capped`/`fallback` shapes), even though — unlike those —
    it is not durably re-shown via `/missed` (no owner_notice row)."""
    monkeypatch.setenv("USER_DELIVERY_RATE_PER_HOUR", "1")
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    assert _deliver(c, "1", "msg one", event_log=ev) == "sent"
    assert _deliver(c, "1", "msg two, over the limit", event_log=ev) == "rate_limited"
    rl = [e for e in ev.events
          if e["kind"] == "user_delivery" and e["attrs"].get("outcome") == "rate_limited"]
    assert len(rl) == 1
    assert rl[0]["attrs"].get("text") == "msg two, over the limit"


def test_daily_cap(monkeypatch):
    monkeypatch.setenv("USER_DELIVERY_RATE_PER_HOUR", "100")
    monkeypatch.setenv("USER_DELIVERY_DAILY_CAP", "3")
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    # two sends an hour+ ago (outside the hourly window, inside the day)
    now = time.time()
    for i, t in enumerate((now - 7200, now - 5400)):
        ev.record("user_delivery", user_id="1", ts=t,
                  attrs={"outcome": "sent", "content_hash": f"old{i}"})
    assert _deliver(c, "1", "third today", event_log=ev) == "sent"
    assert _deliver(c, "1", "fourth today", event_log=ev) == "capped"


def test_no_sink_records_durable_owner_notice():
    ev = _EvLog()
    c = _Container({})
    out = _deliver(c, "1", "important blocker report", event_log=ev)
    assert out == "fallback"
    notices = [e for e in ev.events if e["kind"] == "owner_notice"]
    assert notices and "important blocker report" in notices[0]["attrs"].get("text", "")


def test_failed_send_records_durable_owner_notice():
    ev = _EvLog()
    c = _Container({"telegram_sink": _Sink(ok=False)})
    assert _deliver(c, "1", "report", event_log=ev) == "fallback"
    assert any(e["kind"] == "owner_notice" for e in ev.events)


def test_capped_records_durable_owner_notice(monkeypatch):
    """019 #2: a capped message gets the same durable treatment as fallback —
    exactly one owner_notice (source + truncated text) instead of an
    irrecoverable drop (the 2026-07-18 silently-lost daily digest)."""
    monkeypatch.setenv("USER_DELIVERY_RATE_PER_HOUR", "100")
    monkeypatch.setenv("USER_DELIVERY_DAILY_CAP", "1")
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    assert _deliver(c, "1", "first today", event_log=ev) == "sent"
    out = _deliver(c, "1", "daily digest: 3 goals done, $1.20 spent",
                   event_log=ev, source="cron")
    assert out == "capped"
    assert len(sink.sent) == 1  # the capped message was NOT sent live
    notices = [e for e in ev.events if e["kind"] == "owner_notice"]
    assert len(notices) == 1  # exactly one durable record
    text = notices[0]["attrs"].get("text", "")
    assert "daily digest: 3 goals done" in text  # reconstructable content
    assert "cron" in text                        # source context survives
    # the attempt record also carries the (truncated) text now
    capped = [e for e in ev.events
              if e["kind"] == "user_delivery"
              and e["attrs"].get("outcome") == "capped"]
    assert capped and "daily digest" in capped[0]["attrs"].get("text", "")


def test_empty_text_is_noop():
    ev = _EvLog()
    c = _Container({"telegram_sink": _Sink()})
    assert _deliver(c, "1", "   ", event_log=ev) == "empty"
    assert not ev.events


def test_recipient_override_wins():
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    assert _deliver(c, "1", "to explicit chat", event_log=ev,
                    recipient_override="424242") == "sent"
    assert sink.sent[0][0] == "424242"


# ---------------------------------------------------------------------------
# §3.1 — autonomous send_message routes to the session's own principal
# ---------------------------------------------------------------------------

def _orch(container, user_id="u1", message_router=None, chat_session_key=None):
    from types import SimpleNamespace
    return SimpleNamespace(container=container, user_id=user_id,
                            _message_router=message_router,
                            _chat_session_key=chat_session_key)


def test_autonomous_send_routes_to_own_principal(monkeypatch):
    from core.surfaces.user_delivery import maybe_deliver_autonomous_send
    from agents.task.goals.autonomy_marker import mark_autonomous, _SESSIONS
    _SESSIONS.clear()
    mark_autonomous("sess-goal")
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    out = asyncio.run(maybe_deliver_autonomous_send(
        _orch(c, user_id="12345"), "sess-goal", "blocker: store unavailable",
        event_log=ev))
    assert out == "sent"
    assert sink.sent == [("12345", "blocker: store unavailable")]
    _SESSIONS.clear()


def test_interactive_send_with_live_mirror_is_not_routed():
    """A daemon-resident chat session with a bound router already gets its
    reply delivered by the mirror — routing here too would double-send."""
    from core.surfaces.user_delivery import maybe_deliver_autonomous_send
    from agents.task.goals.autonomy_marker import _SESSIONS
    _SESSIONS.clear()
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    orch = _orch(c, message_router=object(), chat_session_key="chat:1:1")
    out = asyncio.run(maybe_deliver_autonomous_send(
        orch, "sess-chat", "hello", event_log=ev))
    assert out is None
    assert not sink.sent


def test_interactive_send_with_no_live_mirror_routes_via_fallback():
    """2026-08-28 live incident: `polyrob run --resume` rebuilds the
    orchestrator without `_message_router`/`_chat_session_key`, and the
    in-process `is_autonomous` marker never survives the new process either
    — so a resumed chat session's reply had NO delivery path and was
    silently dropped despite send_message reporting success. It must now
    route through the same durable rail an autonomous session uses."""
    from core.surfaces.user_delivery import maybe_deliver_autonomous_send
    from agents.task.goals.autonomy_marker import _SESSIONS
    _SESSIONS.clear()
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    out = asyncio.run(maybe_deliver_autonomous_send(
        _orch(c, user_id="12345"), "sess-resumed-chat", "hello", event_log=ev))
    assert out == "sent"
    assert sink.sent == [("12345", "hello")]


def test_flag_off_disables_routing(monkeypatch):
    monkeypatch.setenv("SEND_MESSAGE_USER_DELIVERY", "false")
    from core.surfaces.user_delivery import maybe_deliver_autonomous_send
    from agents.task.goals.autonomy_marker import mark_autonomous, _SESSIONS
    _SESSIONS.clear()
    mark_autonomous("sess-goal")
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    out = asyncio.run(maybe_deliver_autonomous_send(
        _orch(c), "sess-goal", "text", event_log=ev))
    assert out is None
    assert not sink.sent
    _SESSIONS.clear()


def test_routing_fail_open(monkeypatch):
    """A crash inside the rail must never fail the send_message action."""
    from core.surfaces.user_delivery import maybe_deliver_autonomous_send
    from agents.task.goals.autonomy_marker import mark_autonomous, _SESSIONS
    _SESSIONS.clear()
    mark_autonomous("sess-goal")

    class _Boom:
        def get_service(self, name):
            raise RuntimeError("container exploded")

    out = asyncio.run(maybe_deliver_autonomous_send(
        _orch(_Boom(), user_id="1"), "sess-goal", "text", event_log=_EvLog()))
    assert out in ("failed", "fallback")
    _SESSIONS.clear()


# --- attachments on the rail (QW-1, 2026-07-19) -----------------------------

class _MediaSink:
    """Sink that accepts the media kwarg (MessageRouter shape)."""

    def __init__(self, ok=True):
        self.sent = []
        self._ok = ok

    async def send_message(self, chat_id, text, surface_id="telegram", media=None):
        self.sent.append((chat_id, text, media))
        return self._ok


def test_attachments_threaded_to_media_capable_sink():
    sink, ev = _MediaSink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    entries = [{"kind": "document", "path": "/ws/x402-recon.md", "caption": None}]
    out = _deliver(c, "12345", "done, report attached", event_log=ev,
                   attachments=entries)
    assert out == "sent"
    assert sink.sent == [("12345", "done, report attached", entries)]


def test_attachments_fall_back_to_text_on_legacy_sink():
    """A sink without a media kwarg (the pre-QW-1 shape) still delivers text."""
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    out = _deliver(c, "12345", "done, report attached", event_log=ev,
                   attachments=[{"kind": "document", "path": "/ws/a.md",
                                 "caption": None}])
    assert out == "sent"
    assert sink.sent == [("12345", "done, report attached")]


def test_no_attachments_keeps_legacy_two_arg_call():
    sink, ev = _MediaSink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    out = _deliver(c, "12345", "plain", event_log=ev)
    assert out == "sent"
    assert sink.sent == [("12345", "plain", None)]


def test_attachment_paths_recorded_in_delivery_event():
    """Review Important #3 observability: the user_delivery event must show WHICH
    files rode (or were meant to ride) the message."""
    sink, ev = _MediaSink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    out = _deliver(c, "12345", "with file", event_log=ev,
                   attachments=[{"kind": "document", "path": "/ws/a.md",
                                 "caption": None}])
    assert out == "sent"
    sent_ev = [e for e in ev.events
               if e["kind"] == "user_delivery" and e["attrs"].get("outcome") == "sent"]
    assert sent_ev and sent_ev[0]["attrs"].get("attachments") == ["/ws/a.md"]


# --------------------------------------------------------------------------
# Priority lanes (2026-07-20 overnight incident).
#
# The live failure: the daily cap is a flat FIFO across every source. On the
# night of 07-19 its 30 slots were consumed between 15:01Z and 17:33Z by
# routine chatter (self_evolution goal-start pings 14 + agent_send 11 +
# outbound_open_send 2). From 17:33Z onward EVERYTHING was capped for 12h —
# 99 goal completions (with their 021 deliverables), 2 daily digests, and, at
# 01:38:15Z, the credit sentinel's own "autonomy paused" notice. The owner
# messaged Rob at 02:35Z with no idea it had stopped.
#
# Two lanes fix that class: a CRITICAL lane that the cap cannot touch, and a
# reserved slice that low-value chatter may not consume.
# --------------------------------------------------------------------------


def test_critical_source_bypasses_daily_cap(monkeypatch):
    """The exact 2026-07-20 01:38Z failure: the halt notice must land even when
    routine traffic has already burned every slot in the daily cap."""
    monkeypatch.setenv("USER_DELIVERY_RATE_PER_HOUR", "100")
    monkeypatch.setenv("USER_DELIVERY_DAILY_CAP", "2")
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    assert _deliver(c, "1", "chatter one", event_log=ev) == "sent"
    assert _deliver(c, "1", "chatter two", event_log=ev) == "sent"
    assert _deliver(c, "1", "chatter three", event_log=ev) == "capped"
    out = _deliver(c, "1", "⛔ Autonomy paused: provider credit failure",
                   event_log=ev, source="credit_sentinel")
    assert out == "sent"
    assert sink.sent[-1][1].startswith("⛔ Autonomy paused")


def test_critical_source_bypasses_rate_limit(monkeypatch):
    monkeypatch.setenv("USER_DELIVERY_RATE_PER_HOUR", "1")
    monkeypatch.setenv("USER_DELIVERY_DAILY_CAP", "100")
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    assert _deliver(c, "1", "first", event_log=ev) == "sent"
    assert _deliver(c, "1", "second", event_log=ev) == "rate_limited"
    assert _deliver(c, "1", "⛔ Autonomy paused: credit failure",
                    event_log=ev, source="credit_sentinel") == "sent"


def test_critical_source_is_still_deduped(monkeypatch):
    """Bypassing the cap must not turn the critical lane into a spam channel:
    a byte-identical re-trip inside the dedup window is still suppressed
    (proposal 020 stamps the trip time to keep genuine re-trips distinct)."""
    monkeypatch.setenv("USER_DELIVERY_DAILY_CAP", "100")
    monkeypatch.setenv("USER_DELIVERY_RATE_PER_HOUR", "100")
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    text = "⛔ Autonomy paused (2026-07-20 01:38 UTC): provider credit failure"
    assert _deliver(c, "1", text, event_log=ev, source="credit_sentinel") == "sent"
    assert _deliver(c, "1", text, event_log=ev, source="credit_sentinel") == "deduped"
    assert len(sink.sent) == 1


def test_low_priority_cannot_consume_reserved_slots(monkeypatch):
    """Goal-start pings (low) must leave headroom for completions/digest."""
    monkeypatch.setenv("USER_DELIVERY_RATE_PER_HOUR", "100")
    monkeypatch.setenv("USER_DELIVERY_DAILY_CAP", "5")
    monkeypatch.setenv("USER_DELIVERY_RESERVED_SLOTS", "2")
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    # low-priority traffic may use cap - reserved = 3 slots
    for i in range(3):
        assert _deliver(c, "1", f"▶ goal started: task {i}", event_log=ev,
                        source="self_evolution", priority="low") == "sent"
    assert _deliver(c, "1", "▶ goal started: task 4", event_log=ev,
                    source="self_evolution", priority="low") == "capped"
    # ...and the reserved slots remain available to normal-priority traffic
    assert _deliver(c, "1", "✅ Background goal completed. Deliverables: …",
                    event_log=ev, source="self_evolution") == "sent"
    assert _deliver(c, "1", "daily digest: 3 goals done", event_log=ev,
                    source="cron") == "sent"


def test_reserved_slots_never_starve_low_priority_entirely(monkeypatch):
    """A reserve >= cap would silence low traffic completely; it is clamped so
    at least one slot always remains."""
    monkeypatch.setenv("USER_DELIVERY_RATE_PER_HOUR", "100")
    monkeypatch.setenv("USER_DELIVERY_DAILY_CAP", "2")
    monkeypatch.setenv("USER_DELIVERY_RESERVED_SLOTS", "50")
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    assert _deliver(c, "1", "low one", event_log=ev, priority="low") == "sent"
    assert _deliver(c, "1", "low two", event_log=ev, priority="low") == "capped"


def test_normal_priority_default_is_unchanged_by_the_reserve(monkeypatch):
    """Regression guard: default-priority senders keep the full cap, so the
    reserve is purely a demotion of explicitly-low traffic."""
    monkeypatch.setenv("USER_DELIVERY_RATE_PER_HOUR", "100")
    monkeypatch.setenv("USER_DELIVERY_DAILY_CAP", "3")
    monkeypatch.setenv("USER_DELIVERY_RESERVED_SLOTS", "2")
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    for i in range(3):
        assert _deliver(c, "1", f"normal {i}", event_log=ev) == "sent"
    assert _deliver(c, "1", "normal 4", event_log=ev) == "capped"


def test_lifecycle_pings_have_their_own_smaller_bucket(monkeypatch):
    """2026-08-28 forensics: goal start/done pings (source=self_evolution) filled
    the shared 30/day cap and the agent's own reports were capped — on 08-27 the
    owner got 3 of 171 attempted agent messages. Lifecycle traffic now stops at
    USER_DELIVERY_LIFECYCLE_DAILY_CAP while the agent keeps the rest of the cap."""
    monkeypatch.setenv("USER_DELIVERY_RATE_PER_HOUR", "100")
    monkeypatch.setenv("USER_DELIVERY_DAILY_CAP", "10")
    monkeypatch.setenv("USER_DELIVERY_RESERVED_SLOTS", "0")
    monkeypatch.setenv("USER_DELIVERY_LIFECYCLE_DAILY_CAP", "2")
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    assert _deliver(c, "1", "▶ goal started: a", event_log=ev, source="self_evolution") == "sent"
    assert _deliver(c, "1", "✅ Background goal 'a' completed.", event_log=ev,
                    source="self_evolution") == "sent"
    # third lifecycle ping of the day: capped by the bucket, durably recorded.
    # `self_evolution` is push_owner_message's DEFAULT source and carries real
    # content too, so it keeps its notice; only `source="lifecycle"` is excluded
    # from `/missed` (2026-09-15, C3 — see test_user_delivery_budget.py).
    assert _deliver(c, "1", "▶ goal started: b", event_log=ev, source="self_evolution") == "capped"
    notices = [e for e in ev.events if e["kind"] == "owner_notice"]
    assert notices and "bucket=lifecycle" in notices[-1]["attrs"]["text"]
    capped = [e for e in ev.events if e["kind"] == "user_delivery"
              and e["attrs"].get("outcome") == "capped"]
    assert capped and capped[-1]["attrs"]["text"] == "▶ goal started: b"
    # ...while the agent's own voice still has the shared cap
    for i in range(8):
        assert _deliver(c, "1", f"Treasury run {i} complete", event_log=ev,
                        source="agent_send") == "sent"
    assert _deliver(c, "1", "Treasury run 9 complete", event_log=ev,
                    source="agent_send") == "capped"


def test_lifecycle_bucket_zero_disables_it(monkeypatch):
    monkeypatch.setenv("USER_DELIVERY_RATE_PER_HOUR", "100")
    monkeypatch.setenv("USER_DELIVERY_DAILY_CAP", "5")
    monkeypatch.setenv("USER_DELIVERY_RESERVED_SLOTS", "0")
    monkeypatch.setenv("USER_DELIVERY_LIFECYCLE_DAILY_CAP", "0")
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    for i in range(5):
        assert _deliver(c, "1", f"▶ goal started: {i}", event_log=ev,
                        source="self_evolution") == "sent"
    assert _deliver(c, "1", "▶ goal started: 6", event_log=ev,
                    source="self_evolution") == "capped"


# ---------------------------------------------------------------------------
# 044 T20 fix round 2 (Obs 1): a PUBLIC room reply never mirrors to the owner DM
# ---------------------------------------------------------------------------

def test_a_room_session_never_mirrors_to_the_owner_dm():
    """A room SERVICE run is AUTONOMOUS and has a live mirror, so it fell
    through the `has_live_mirror and not is_autonomous` carve-out: every public
    answer was ALSO pushed into the owner's private chat, spending his daily
    delivery budget on a message he had already read in the room."""
    from agents.task.goals.autonomy_marker import _SESSIONS, mark_autonomous
    from core.surfaces.user_delivery import maybe_deliver_autonomous_send
    _SESSIONS.clear()
    mark_autonomous("sess-room")
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    orch = _orch(c, user_id="12345", message_router=object(),
                 chat_session_key="agent:main:telegram:supergroup:-1001")
    orch._public_session = True
    out = asyncio.run(maybe_deliver_autonomous_send(
        orch, "sess-room", "yes, live since Tuesday", event_log=ev))
    assert out is None
    assert not sink.sent
    _SESSIONS.clear()


def test_a_private_autonomous_send_still_reaches_the_owner():
    """Polarity guard: the carve-out this narrows exists for a goal/cron run with
    no surface of its own, and that must keep working."""
    from agents.task.goals.autonomy_marker import _SESSIONS, mark_autonomous
    from core.surfaces.user_delivery import maybe_deliver_autonomous_send
    _SESSIONS.clear()
    mark_autonomous("sess-goal")
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    orch = _orch(c, user_id="12345")
    orch._public_session = False
    out = asyncio.run(maybe_deliver_autonomous_send(
        orch, "sess-goal", "the build is green", event_log=ev))
    assert out == "sent"
    assert sink.sent == [("12345", "the build is green")]
    _SESSIONS.clear()


def test_terminal_attached_send_is_not_routed():
    """2026-09-17: the REPL renders its replies from the feed but binds no
    router, so every chat reply went through the owner rail; past the hourly
    rate limit the model was told its answer was NOT delivered and re-sent."""
    from core.surfaces.binding import bind_terminal_surface
    from core.surfaces.user_delivery import maybe_deliver_autonomous_send
    from agents.task.goals.autonomy_marker import _SESSIONS
    _SESSIONS.clear()
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    orch = _orch(c, user_id="12345")
    bind_terminal_surface(orch)
    out = asyncio.run(maybe_deliver_autonomous_send(
        orch, "sess-repl", "hello", event_log=ev))
    assert out is None
    assert not sink.sent


def test_terminal_attached_autonomous_session_still_routes():
    """A goal run that happens inside a REPL process has no human reading the
    feed for it — the marker is per-orchestrator, and an autonomous session
    keeps the durable rail."""
    from core.surfaces.binding import bind_terminal_surface
    from core.surfaces.user_delivery import maybe_deliver_autonomous_send
    from agents.task.goals.autonomy_marker import mark_autonomous, _SESSIONS
    _SESSIONS.clear()
    mark_autonomous("sess-goal")
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    orch = _orch(c, user_id="12345")
    bind_terminal_surface(orch)
    out = asyncio.run(maybe_deliver_autonomous_send(
        orch, "sess-goal", "blocker", event_log=ev))
    assert out == "sent"
    _SESSIONS.clear()
