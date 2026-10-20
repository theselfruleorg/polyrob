"""084 WS4: what the agent reads about the owner's own sends is true.

Prod 2026-10-19: after a 7-card USDG payout the agent called the owner's sends
"autonomous runs" (owner_verb rows rendered "you→owner") and said a refused
send had "landed" (the delta kept the OLDEST 20 rows and lost the newest).
"""
from types import SimpleNamespace

from core.surfaces import owner_thread as ot
from core.surfaces.owner_thread import OwnerThreadStore


def _container(st):
    return SimpleNamespace(get_service=lambda name: st)


def test_the_delta_keeps_the_newest_rows_and_names_the_gap(tmp_path):
    st = OwnerThreadStore(str(tmp_path / "owner_thread.db"))
    for i in range(30):
        st.record("out", "u", f"send {i}", session_id="mine", source="owner_verb",
                  now=100.0 + i)
    rows = ot.thread_delta(_container(st), "u", session_id="mine", since_ts=50.0)
    assert [r["body"] for r in rows] == [f"send {i}" for i in range(10, 30)]
    assert rows[0]["_earlier"] == 10
    block = ot.render_block(rows, kind="delta", this_session="mine", max_chars=100_000)
    assert "[10 earlier lines omitted" in block
    assert "send 29" in block and "send 9\n" not in block


def test_an_owner_verb_reply_never_reads_as_sent_by_the_agent():
    line = ot.render_line({"direction": "out", "body": "SENT AND CONFIRMED 24 USDG",
                           "source": "owner_verb", "via": "telegram", "ts": 0,
                           "session_id": "mine"}, this_session="mine")
    assert "you→owner" not in line
    assert "owner's OWN command or card tap" in line and "NOT sent by you" in line


def test_a_card_tap_and_a_command_read_as_the_owners_acts():
    tap = ot.render_line({"direction": "in", "body": "/send 24 x to y go",
                          "kind": "card_tap", "ts": 0})
    cmd = ot.render_line({"direction": "in", "body": "/send 18 x to y go",
                          "kind": "command", "ts": 0})
    assert "owner tapped a card: /send 24" in tap
    assert "owner typed a command: /send 18" in cmd


def test_the_delta_heading_names_the_owners_own_acts():
    block = ot.render_block([{"direction": "in", "body": "hi", "ts": 0}], kind="delta")
    assert "owner's own" in block and "OTHER sessions and rails exchanged" not in block


def test_a_notice_from_a_non_autonomous_session_is_not_called_autonomous(monkeypatch):
    from core import status_render
    import core.autonomy_marker as am
    monkeypatch.setattr(am, "is_autonomous", lambda sid: sid == "auto1")
    assert status_render._notice_author({"session_id": "auto1"}) == "an autonomous run"
    assert status_render._notice_author({"session_id": "chat2"}) == "another chat session"
    assert status_render._notice_author({}) == "a rail or an owner command"
