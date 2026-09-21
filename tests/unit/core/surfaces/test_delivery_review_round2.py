"""Second-pass review of the 2026-09-15 communication fixes.

Four defects the first pass introduced or left behind. Each one is the same
shape as the bug it sits next to, which is why they were easy to miss:
a fix applied to ONE branch of a set.
"""
import asyncio
import time

import pytest


class _EvLog:
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

    def notices(self):
        return [e["attrs"].get("text") or "" for e in self.query(kind="owner_notice")]

    def rows(self, outcome):
        return [e for e in self.query(kind="user_delivery")
                if e["attrs"].get("outcome") == outcome]


class _Sink:
    def __init__(self, ok=True):
        self.sent = []
        self._ok = ok

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        return self._ok


class _Container:
    def __init__(self, services):
        self._s = services

    def get_service(self, name):
        return self._s.get(name)


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    return tmp_path


def _deliver(container, text, **kw):
    from core.surfaces.user_delivery import deliver_user_message
    return asyncio.run(deliver_user_message(container, "12345", text, **kw))


# --- R1: the recorded lane must be the EFFECTIVE lane -----------------------
#
# C1 stamps `lane` on every row and excludes the critical lane from the cap
# window. But `_record` derived the lane from the SOURCE alone, ignoring the
# explicit `priority=` argument the rail itself honours one line earlier. So a
# caller passing priority="critical" skipped the cap for itself and then spent
# a slot anyway — the exact bug C1 exists to remove, surviving on the other
# half of `resolve_priority`.

def test_an_explicitly_critical_send_does_not_consume_the_cap(monkeypatch):
    monkeypatch.setenv("USER_DELIVERY_DAILY_CAP", "3")
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    for i in range(3):
        assert _deliver(c, f"urgent {i}", source="agent_send",
                        priority="critical", event_log=ev) == "sent"
    assert _deliver(c, "an ordinary report", source="agent_send", event_log=ev) == "sent"


def test_the_recorded_lane_reflects_the_explicit_priority():
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    _deliver(c, "a start ping", source="agent_send", priority="low", event_log=ev)
    assert ev.rows("sent")[0]["attrs"]["lane"] == "low"


def test_an_explicitly_low_send_still_spends_its_reduced_allowance(monkeypatch):
    monkeypatch.setenv("USER_DELIVERY_DAILY_CAP", "4")
    monkeypatch.setenv("USER_DELIVERY_RESERVED_SLOTS", "2")
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    for i in range(2):
        assert _deliver(c, f"chatter {i}", source="agent_send", priority="low",
                        event_log=ev) == "sent"
    assert _deliver(c, "chatter 3", source="agent_send", priority="low",
                    event_log=ev) == "capped"


# --- R2: one notice per body, for EVERY suppression shape -------------------
#
# C9 gave the `paused` branch a one-notice-per-body rule. Its four siblings
# (capped, rate_limited, undelivered fallback, cooldown) did not get it — and
# C2 made `fallback` retryable, so a dead sink now re-writes its notice on
# every attempt. `/missed` shows five entries; filling them with one repeated
# body is the same failure C3 just fixed.

def test_a_repeated_undelivered_body_writes_one_notice():
    ev = _EvLog()
    for _ in range(4):
        assert _deliver(None, "the blocker report", source="agent_send",
                        event_log=ev) == "no_sink"
    assert len([n for n in ev.notices() if "the blocker report" in n]) == 1
    assert len(ev.rows("no_sink")) == 4       # every attempt is still audited


def test_a_repeated_capped_body_writes_one_notice(monkeypatch):
    monkeypatch.setenv("USER_DELIVERY_DAILY_CAP", "1")
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    _deliver(c, "first", source="agent_send", event_log=ev)
    for _ in range(3):
        assert _deliver(c, "SCOUT-ENTRY blocked", source="agent_send",
                        event_log=ev) == "capped"
    assert len([n for n in ev.notices() if "SCOUT-ENTRY blocked" in n]) == 1


def test_a_different_body_still_gets_its_own_notice(monkeypatch):
    monkeypatch.setenv("USER_DELIVERY_DAILY_CAP", "1")
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    _deliver(c, "first", source="agent_send", event_log=ev)
    _deliver(c, "report A", source="agent_send", event_log=ev)
    _deliver(c, "report B", source="agent_send", event_log=ev)
    assert len(ev.notices()) == 2


@pytest.mark.parametrize("outcome,blocks", [
    ("capped", True), ("paused", True), ("fallback", True),
    ("rate_limited", True), ("cooldown", True),
    # None of these wrote a notice, so none of them may suppress one. A `sent`
    # row in particular must NOT: a later suppression of the same body is a new
    # thing the owner did not receive.
    ("sent", False), ("deduped", False), ("quiet_held", False),
])
def test_only_a_notice_producing_outcome_suppresses_the_next_notice(outcome, blocks):
    from core.surfaces.user_delivery import _notice_already_written
    ev = _EvLog()
    ev.record("user_delivery", user_id="12345",
              attrs={"outcome": outcome, "content_hash": "abc"})
    assert _notice_already_written(ev, "12345", "abc", time.time()) is blocks


def test_notice_dedup_fails_open_to_writing():
    """An unreadable log costs a duplicate entry, never a lost one."""
    from core.surfaces.user_delivery import _notice_already_written

    class _Broken:
        def query(self, **kw):
            raise RuntimeError("log down")

    assert _notice_already_written(_Broken(), "12345", "abc", time.time()) is False
