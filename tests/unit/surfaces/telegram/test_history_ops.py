"""061 — `/thread`: the ONE owner transcript, honest when empty or unreadable."""
import time

from core.surfaces import owner_thread as ot
from surfaces.telegram.history_ops import history_reply


def test_no_store_is_named_not_empty(tmp_path):
    out = history_reply("12345", str(tmp_path), [])
    assert "no conversation recorded yet" in out


def test_transcript_newest_last_with_rails(tmp_path):
    ot.record_owner_out(None, "12345", "Exit rail ran. Pick A or B.", via="telegram", session_id="r",
                        source="agent_send", rail_id="cron:exit", now=time.time() - 60)
    # the seam needs a home: write through a data_dir-bearing container
    class C:
        config = type("Cfg", (), {"data_dir": str(tmp_path)})()
        def get_service(self, n): return None
        def register_service(self, n, s): pass
    ot.record_owner_out(C(), "12345", "Exit rail ran. Pick A or B.", via="telegram", session_id="r",
                        source="agent_send", rail_id="cron:exit", now=time.time() - 60)
    ot.record_owner_in(C(), "12345", "Explin better", via="telegram", session_id="d", now=time.time() - 30)
    out = history_reply("12345", str(tmp_path), [])
    lines = out.splitlines()
    assert lines[0].startswith("our conversation — last 2 line(s)")
    assert "rob (via telegram, agent_send): Exit rail ran" in lines[1]
    assert lines[2].endswith("you: Explin better")
    assert history_reply("12345", str(tmp_path), ["1"]).count("\n") == 1
    assert "no lines in the last 1h" not in history_reply("12345", str(tmp_path), ["1h"])


def test_owner_only_and_flag_off(tmp_path, monkeypatch):
    assert "Only the owner" in history_reply(None, str(tmp_path), [])
    monkeypatch.setenv("OWNER_THREAD_ENABLED", "off")
    assert "OWNER_THREAD_ENABLED" in history_reply("12345", str(tmp_path), [])
