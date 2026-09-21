"""Delivery-rail budget + record honesty (prod review 2026-09-15).

Four defects this pins, all measured on Rob #1 prod over 2026-09-08..15
in the delivery-suppression review:

* **C1** the shared daily cap counted CRITICAL-lane sends, which are exempt from
  it — so on 2026-09-14 sixteen `tx_execution` notices plus nine approval prompts
  spent the owner's whole 30-slot budget and the agent's own voice got ONE send.
* **C2** a `deduped` row carried no text and wrote no notice, so 383 of 383 were
  irrecoverable; and dedup matched an `undelivered` fallback, which made a failed
  send permanent (the self-sealing class).
* **C3/C9** framework lifecycle chatter wrote 822 of the 899 rows in the owner's
  `/missed` store, and a paused body re-wrote its notice on every tick.
* **C7** a `sent` row carried no text either, so nothing could answer "what did
  you actually tell me".
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

    def outcomes(self, user_id="12345"):
        return [e["attrs"].get("outcome")
                for e in sorted(self.query(kind="user_delivery", user_id=user_id),
                                key=lambda e: e["ts"])]

    def notices(self, user_id="12345"):
        return [e["attrs"].get("text") or ""
                for e in self.query(kind="owner_notice", user_id=user_id)]


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


def _deliver(container, user_id, text, **kw):
    from core.surfaces.user_delivery import deliver_user_message
    return asyncio.run(deliver_user_message(container, user_id, text, **kw))


# --- C1: a lane that cannot be denied must not be able to deny others --------

def test_critical_sends_do_not_consume_the_shared_daily_cap(monkeypatch):
    monkeypatch.setenv("USER_DELIVERY_DAILY_CAP", "3")
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    for i in range(3):
        assert _deliver(c, "12345", f"tx {i} broadcast", source="tx_execution",
                        event_log=ev) == "sent"
    # The agent's own report must still fit: the three above were cap-exempt.
    assert _deliver(c, "12345", "SCOUT-ENTRY blocked at the buy step",
                    source="agent_send", event_log=ev) == "sent"


def test_normal_sends_still_consume_the_shared_daily_cap(monkeypatch):
    monkeypatch.setenv("USER_DELIVERY_DAILY_CAP", "2")
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    assert _deliver(c, "12345", "report one", source="agent_send", event_log=ev) == "sent"
    assert _deliver(c, "12345", "report two", source="agent_send", event_log=ev) == "sent"
    assert _deliver(c, "12345", "report three", source="agent_send", event_log=ev) == "capped"


def test_critical_sends_do_not_consume_the_hourly_rate_limit(monkeypatch):
    monkeypatch.setenv("USER_DELIVERY_RATE_PER_HOUR", "2")
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    for i in range(2):
        assert _deliver(c, "12345", f"approve item {i}", source="approval",
                        event_log=ev) == "sent"
    assert _deliver(c, "12345", "a real report", source="agent_send",
                    event_log=ev) == "sent"


# --- C2: a suppressed message is never irrecoverable ------------------------

def test_dedup_does_not_match_an_undelivered_fallback():
    """The self-sealing class: a send with no sink records `no_sink`, and the
    retry used to be `deduped` forever — the owner never saw either."""
    ev = _EvLog()
    assert _deliver(None, "12345", "the blocker report", source="agent_send",
                    event_log=ev) == "no_sink"
    sink = _Sink()
    c = _Container({"telegram_sink": sink})
    assert _deliver(c, "12345", "the blocker report", source="agent_send",
                    event_log=ev) == "sent"
    assert sink.sent and sink.sent[0][1] == "the blocker report"


def test_dedup_still_matches_a_delivered_send():
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    assert _deliver(c, "12345", "same body", source="agent_send", event_log=ev) == "sent"
    assert _deliver(c, "12345", "same body", source="agent_send", event_log=ev) == "deduped"


def test_deduped_row_carries_the_text():
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    _deliver(c, "12345", "approve the payout", source="approval", event_log=ev)
    _deliver(c, "12345", "approve the payout", source="approval", event_log=ev)
    row = [e for e in ev.query(kind="user_delivery", user_id="12345")
           if e["attrs"].get("outcome") == "deduped"][0]
    assert row["attrs"].get("text") == "approve the payout"


def test_rate_limited_is_readable_in_missed(monkeypatch):
    monkeypatch.setenv("USER_DELIVERY_RATE_PER_HOUR", "1")
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    assert _deliver(c, "12345", "first", source="agent_send", event_log=ev) == "sent"
    assert _deliver(c, "12345", "second and urgent", source="agent_send",
                    event_log=ev) == "rate_limited"
    from core.surfaces.user_delivery import NOTICE_MARKERS
    assert any(n.startswith(NOTICE_MARKERS[3]) and "second and urgent" in n
               for n in ev.notices())


def test_missed_reads_every_notice_marker():
    """`core.surfaces.missed` must know every marker the rail can write —
    an unmatched marker is a notice no seat can render."""
    from core.surfaces.missed import _KIND_BY_MARKER
    from core.surfaces.user_delivery import NOTICE_MARKERS
    assert set(NOTICE_MARKERS) == set(_KIND_BY_MARKER)


# --- C7: record what was actually delivered ---------------------------------

def test_sent_row_carries_the_text():
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    _deliver(c, "12345", "the answer the owner received", source="agent_send", event_log=ev)
    row = [e for e in ev.query(kind="user_delivery", user_id="12345")
           if e["attrs"].get("outcome") == "sent"][0]
    assert row["attrs"].get("text") == "the answer the owner received"


# --- C3: lifecycle chatter stays out of the owner's recovery store ----------

def test_capped_lifecycle_ping_writes_no_owner_notice(monkeypatch):
    monkeypatch.setenv("USER_DELIVERY_LIFECYCLE_DAILY_CAP", "1")
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    assert _deliver(c, "12345", "▶ goal started: one", source="lifecycle",
                    event_log=ev) == "sent"
    assert _deliver(c, "12345", "▶ goal started: two", source="lifecycle",
                    event_log=ev) == "capped"
    assert ev.notices() == []
    # …but the attempt itself is still durably recorded, with its text.
    row = [e for e in ev.query(kind="user_delivery", user_id="12345")
           if e["attrs"].get("outcome") == "capped"][0]
    assert row["attrs"].get("text") == "▶ goal started: two"


def test_capped_agent_report_still_writes_an_owner_notice(monkeypatch):
    monkeypatch.setenv("USER_DELIVERY_DAILY_CAP", "1")
    sink, ev = _Sink(), _EvLog()
    c = _Container({"telegram_sink": sink})
    _deliver(c, "12345", "first", source="agent_send", event_log=ev)
    assert _deliver(c, "12345", "SCOUT-ENTRY blocked", source="agent_send",
                    event_log=ev) == "capped"
    assert any("SCOUT-ENTRY blocked" in n for n in ev.notices())


# --- C9: a held body does not re-write its notice every tick ----------------

def test_paused_body_writes_one_notice_not_one_per_tick(tmp_path, monkeypatch):
    from core import autonomy_control as ac
    ac.pause(str(tmp_path), scopes=("pings",), via="test")
    ev = _EvLog()
    body = "🧠 I've proposed 3 change(s) to how I work"
    for _ in range(4):
        assert _deliver(None, "12345", body, source="goal_blocked",
                        event_log=ev) == "paused"
    assert len([n for n in ev.notices() if body in n]) == 1
    assert ev.outcomes().count("paused") == 4
