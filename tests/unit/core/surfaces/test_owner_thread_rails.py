"""061 §4.5 — every rail to the owner writes the ONE thread; every owner seat
records the owner's line. The rail set is enumerated HERE (imported outcome
sets, not copies) so a new owner-facing outcome must add itself or fail."""
import asyncio
import time

import pytest

from core.surfaces import owner_thread as ot
from core.surfaces.user_delivery import _DELIVERED_OUTCOMES, deliver_user_message


class _EvLog:
    def __init__(self):
        self.events = []

    def record(self, kind, *, user_id="", session_id="", source="", ts=None, attrs=None, **kw):
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


class _SinkEx:
    """A sink that names the message id (the 061 Telegram sink shape)."""
    def __init__(self, outcome="sent"):
        self.sent = []
        self.outcome = outcome

    async def send_message(self, chat_id, text, media=None):
        return (await self.send_message_ex(chat_id, text, media=media))["outcome"] == "sent"

    async def send_message_ex(self, chat_id, text, media=None):
        self.sent.append((chat_id, text))
        return {"outcome": self.outcome, "message_id": "777" if self.outcome == "sent" else None}


class _BoolSink:
    def __init__(self, ok=True):
        self.ok = ok

    async def send_message(self, chat_id, text, media=None):
        return self.ok


class _Cfg:
    def __init__(self, d):
        self.data_dir = d


class _Container:
    def __init__(self, data_dir, services):
        self.config = _Cfg(str(data_dir))
        self._s = dict(services)

    def get_service(self, name):
        return self._s.get(name)

    def register_service(self, name, svc):
        self._s[name] = svc


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    return tmp_path


def _deliver(container, text, **kw):
    return asyncio.run(deliver_user_message(container, "12345", text, **kw))


def test_delivered_outcome_set_is_the_one_the_thread_records():
    # The rail records `sent` (and the outbox-owned `queued`); anything else is
    # a line the owner did not see and stays in /missed.
    assert set(_DELIVERED_OUTCOMES) == ot.DELIVERED_OUTCOMES


def test_rail_a_sent_records_via_mid_and_session(tmp_path):
    ev = _EvLog()
    c = _Container(tmp_path, {"telegram_sink": _SinkEx()})
    assert _deliver(c, "Exit rail ran. Pick A or B.", source="agent_send",
                    session_id="cron-run-1", event_log=ev) == "sent"
    rows = ot.thread_tail(c, "12345")
    assert len(rows) == 1
    r = rows[0]
    assert (r["via"], r["mid"], r["session_id"], r["source"]) == ("telegram", "777", "cron-run-1", "agent_send")


def test_rail_a_bool_sink_records_without_mid(tmp_path):
    ev = _EvLog()
    c = _Container(tmp_path, {"telegram_sink": _BoolSink()})
    assert _deliver(c, "a plain notice", source="cron", event_log=ev) == "sent"
    r = ot.thread_tail(c, "12345")[0]
    assert r["mid"] == "" and r["via"] == "telegram" and r["source"] == "cron"


@pytest.mark.parametrize("scenario", ["deduped", "failed", "no_sink"])
def test_rail_a_undelivered_outcomes_record_nothing(tmp_path, scenario):
    ev = _EvLog()
    if scenario == "no_sink":
        c = _Container(tmp_path, {})
        out = _deliver(c, "nobody to tell", source="cron", event_log=ev)
        assert out == "no_sink"
    elif scenario == "failed":
        c = _Container(tmp_path, {"telegram_sink": _BoolSink(ok=False)})
        out = _deliver(c, "send refused", source="cron", event_log=ev)
        assert out == "fallback"
    else:
        c = _Container(tmp_path, {"telegram_sink": _SinkEx()})
        assert _deliver(c, "twice", source="cron", event_log=ev) == "sent"
        assert _deliver(c, "twice", source="cron", event_log=ev) == "deduped"
        assert len(ot.thread_tail(c, "12345")) == 1
        return
    assert ot.thread_tail(c, "12345") == []


def test_rail_a_ask_id_rides_the_row(tmp_path):
    ev = _EvLog()
    c = _Container(tmp_path, {"telegram_sink": _SinkEx()})
    assert _deliver(c, "I need your decision (ask abc): A or B?", source="owner_ask",
                    session_id="run", event_log=ev, ask_id="abc123") == "sent"
    assert ot.thread_tail(c, "12345")[0]["ask_id"] == "abc123"


def test_rail_a_stamps_the_rail_id_of_an_autonomous_session(tmp_path):
    from agents.task.goals.autonomy_marker import mark_autonomous
    mark_autonomous("cron-sess-9", None, cron_job_id="job9")
    ev = _EvLog()
    c = _Container(tmp_path, {"telegram_sink": _SinkEx()})
    assert _deliver(c, "the exit rail spoke", source="agent_send",
                    session_id="cron-sess-9", event_log=ev) == "sent"
    assert ot.thread_tail(c, "12345")[0]["rail_id"] == "cron:job9"


def test_quiet_release_lands_once_through_the_sent_branch(tmp_path, monkeypatch):
    # A released hold re-enters deliver_user_message → the `sent` branch. No
    # second hook, no second row.
    from core.surfaces.user_delivery import release_quiet_held
    ev = _EvLog()
    c = _Container(tmp_path, {"telegram_sink": _SinkEx()})
    ev.record("user_delivery", user_id="12345", source="cron", ts=time.time() - 60,
              attrs={"outcome": "quiet_held", "content_hash": "h1", "held_text": "held body"})
    monkeypatch.setattr("core.surfaces.quiet_hours.quiet_window_active", lambda uid, home: False)
    released = asyncio.run(release_quiet_held(c, event_log=ev))
    assert released == 1
    rows = ot.thread_tail(c, "12345")
    assert [r["body"] for r in rows] == ["held body"]


# --- the interactive reply latch --------------------------------------------

class _Orch:
    def __init__(self, container, *, key=None, terminal=False, public=False, sid="s-int"):
        self.container = container
        self.user_id = "12345"
        self.session_id = sid
        self._chat_session_key = key
        self._terminal_attached = terminal
        self._public_session = public


def test_turn_latch_records_interactive_reply_with_seat_via(tmp_path):
    from core.surfaces.turn_reply import mark_reply_published
    c = _Container(tmp_path, {})
    mark_reply_published(_Orch(c, key="agent:main:telegram:dm:1:12345"), "a telegram answer")
    mark_reply_published(_Orch(c, terminal=True, sid="s-repl"), "a repl answer")
    mark_reply_published(_Orch(c, sid="s-con"), "a console answer")
    rows = ot.thread_recent(c, "12345", limit=10)
    assert [(r["via"], r["body"]) for r in rows] == [
        ("telegram", "a telegram answer"), ("repl", "a repl answer"), ("console", "a console answer")]
    assert all(r["source"] == "reply" for r in rows)


def test_turn_latch_skips_rooms_and_autonomous_sessions(tmp_path):
    from agents.task.goals.autonomy_marker import mark_autonomous
    from core.surfaces.turn_reply import mark_reply_published
    c = _Container(tmp_path, {})
    mark_reply_published(_Orch(c, public=True), "a room answer")
    mark_autonomous("s-auto-1", "goal1")
    mark_reply_published(_Orch(c, sid="s-auto-1"), "an autonomous answer")
    assert ot.thread_recent(c, "12345", limit=10) is None


# --- the owner seats ---------------------------------------------------------

def test_repl_line_is_recorded(tmp_path):
    from agents.task.agent.conversation import _record_repl_line

    class _Agent:
        container = _Container(tmp_path, {})
        user_id = "12345"
        session_id = "s-repl"
    _record_repl_line(_Agent(), "hello from the terminal")
    r = ot.thread_recent(_Agent.container, "12345", limit=5)[0]
    assert (r["direction"], r["via"], r["session_id"]) == ("in", "repl", "s-repl")


def test_api_and_console_lines_are_recorded_with_their_seat(tmp_path):
    from api.task_http_api import _record_owner_thread_line

    class _Req:
        def __init__(self, url):
            self.url = url
            self.headers = {}

    class _Msg:
        text = "from the console"
        kind = "comment"

    class _Agent:
        container = _Container(tmp_path, {})

        def get_orchestrator(self, sid):
            return None
    _record_owner_thread_line(_Req("http://x/api/session/abc/messages"), _Agent(), "abc", "12345", _Msg())
    _Msg.text = "from the api"
    _record_owner_thread_line(_Req("http://x/api/tasks/sessions/abc/messages"), _Agent(), "abc", "12345", _Msg())
    rows = ot.thread_recent(_Agent.container, "12345", limit=5)
    assert [(r["via"], r["body"]) for r in rows] == [("console", "from the console"), ("api", "from the api")]


def test_telegram_owner_line_records_mid_reply_to_and_skips_rooms_and_readonly_commands(tmp_path):
    from surfaces.telegram.harness import _record_owner_line

    class _Src:
        chat_type = "dm"

    class _Ident:
        user_id = "12345"
        source = _Src()

    class _Inbound:
        def __init__(self, text, reply_to=None, mid="55", chat_type="dm"):
            self.text = text
            self.reply_to = reply_to
            self.raw = {"message": {"message_id": mid}}
            self.media = []
            self.identity = _Ident()
            self.identity.source = type("S", (), {"chat_type": chat_type})()

    class _Decision:
        def __init__(self, command=None):
            self.command = command
            self.tier = None

    class _Result:
        def __init__(self, inbound, command=None):
            self.inbound = inbound
            self.decision = _Decision(command)

    class _TA:
        container = _Container(tmp_path, {})
    ta = _TA()
    _record_owner_line(ta, _Result(_Inbound("Explain better", reply_to="777")), session_id="s1", kind="comment")
    _record_owner_line(ta, _Result(_Inbound("/status", mid="56"), command="/status"), session_id="", kind="command")
    _record_owner_line(ta, _Result(_Inbound("/approve x", mid="57"), command="/approve"), session_id="", kind="command")
    _record_owner_line(ta, _Result(_Inbound("room chatter", mid="58", chat_type="group")), session_id="s2", kind="comment")
    rows = ot.thread_recent(ta.container, "12345", limit=10)
    assert [(r["body"], r["mid"], r["reply_to_mid"], r["kind"]) for r in rows] == [
        ("Explain better", "55", "777", "comment"), ("/approve x", "57", "", "command")]


def test_message_tool_owner_branch_records_only_autonomous_sends(tmp_path):
    from agents.task.goals.autonomy_marker import mark_autonomous
    from tools.controller.message_send import _is_autonomous_session
    mark_autonomous("s-auto-2", "goal2")
    assert _is_autonomous_session("s-auto-2") is True
    assert _is_autonomous_session("s-interactive") is False


def test_cooldown_reads_the_thread_across_rails(tmp_path, monkeypatch):
    """A framework notice (rail A) now counts for the autonomous owner-resend
    cooldown — before 061 the guard read only the `message` tool's own rows."""
    from tools.controller.turn_origin import _autonomous_owner_resend_cooldown_refusal
    monkeypatch.setenv("OWNER_MESSAGE_COOLDOWN_SEC", "7200")
    ev = _EvLog()
    c = _Container(tmp_path, {"telegram_sink": _SinkEx()})
    assert _deliver(c, "the same blocker report", source="cron", event_log=ev) == "sent"

    class _Ctx:
        is_sub_agent = True
        role = "leaf"

    class _Ctl:
        orchestrator = None
    res = _autonomous_owner_resend_cooldown_refusal(
        _Ctx(), _Ctl(), container=c, user_id="12345", surface="telegram",
        target="telegram:28436760", owner_targets={"telegram": "28436760"},
        text="the same blocker report", event_log=ev)
    assert res is not None and "already reached the owner" in res.extracted_content
    res2 = _autonomous_owner_resend_cooldown_refusal(
        _Ctx(), _Ctl(), container=c, user_id="12345", surface="telegram",
        target="28436760", owner_targets={"telegram": "28436760"},
        text="a materially new report", event_log=ev)
    assert res2 is None


# --- 061 alignment (2026-09-22): only the OWNER's lines are thread lines -------

def test_rail_a_override_to_a_non_owner_is_not_a_thread_line(tmp_path, monkeypatch):
    """A cron job's `deliver_target` can be a room or another chat; the rail
    still sends and records telemetry, but the owner thread stays clean."""
    monkeypatch.setattr("core.surfaces.user_delivery._resolve_recipient", lambda c, uid: "12345")
    ev = _EvLog()
    c = _Container(tmp_path, {"telegram_sink": _SinkEx()})
    assert _deliver(c, "a room report", source="cron", event_log=ev,
                    recipient_override="-100777") == "sent"
    assert ot.thread_tail(c, "12345") == []
    # the owner's own address under another spelling IS the owner
    assert _deliver(c, "an owner report", source="cron", event_log=ev,
                    recipient_override="telegram:12345") == "sent"
    assert [r["body"] for r in ot.thread_tail(c, "12345")] == ["an owner report"]


def test_telegram_harness_records_the_true_surface(tmp_path):
    from surfaces.telegram.harness import _record_owner_line

    class _Src:
        surface_id = "slack"
        chat_type = "dm"

    class _Inbound:
        text = "from slack"
        reply_to = None
        raw = {}
        media = []
        identity = type("I", (), {"user_id": "12345", "source": _Src()})()

    class _Result:
        inbound = _Inbound()
        decision = type("D", (), {"command": None, "tier": "owner"})()

    class _TA:
        container = _Container(tmp_path, {})
    ta = _TA()
    _record_owner_line(ta, _Result(), session_id="s1", kind="comment")
    assert ot.thread_recent(ta.container, "12345", limit=5)[0]["via"] == "slack"


def test_chat_once_records_the_tenants_line_never_anonymous(tmp_path):
    from agents.task.task_agent_chat import _record_chat_once_owner_line
    from core.identity import ANON_USER_ID

    class _Agent:
        container = _Container(tmp_path, {})
    _record_chat_once_owner_line(_Agent(), ANON_USER_ID, "anonymous", "s1")
    assert ot.thread_recent(_Agent.container, ANON_USER_ID, limit=5) is None
    _record_chat_once_owner_line(_Agent(), "owner-1", "hello over the api", "s2")
    r = ot.thread_recent(_Agent.container, "owner-1", limit=5)[0]
    assert (r["via"], r["session_id"], r["direction"]) == ("api", "s2", "in")


def test_turn_latch_maps_chat_once_keys_to_api(tmp_path):
    from core.surfaces.turn_reply import mark_reply_published
    c = _Container(tmp_path, {})
    mark_reply_published(_Orch(c, key="chat:owner-1:web", sid="s-api"), "an api answer")
    assert ot.thread_recent(c, "12345", limit=5)[0]["via"] == "api"
