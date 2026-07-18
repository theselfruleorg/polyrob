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

def _orch(container, user_id="u1"):
    from types import SimpleNamespace
    return SimpleNamespace(container=container, user_id=user_id)


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


def test_interactive_send_is_not_routed():
    from core.surfaces.user_delivery import maybe_deliver_autonomous_send
    from agents.task.goals.autonomy_marker import _SESSIONS
    _SESSIONS.clear()
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    out = asyncio.run(maybe_deliver_autonomous_send(
        _orch(c), "sess-chat", "hello", event_log=ev))
    assert out is None
    assert not sink.sent


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
