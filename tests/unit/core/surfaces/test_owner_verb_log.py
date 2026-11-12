"""116 W3.7: an owner verb leaves a record — one event, no arguments, no body;
the last /status text per seat is kept (one entry, overwritten)."""
import json
import os
from types import SimpleNamespace

import pytest

from core.surfaces import owner_verb_log as ovl


@pytest.fixture
def home(tmp_path, monkeypatch):
    import core.event_log as el
    monkeypatch.setattr(el, "_INSTANCES", {})
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "telemetry_events.db"))
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "true")
    return str(tmp_path)


def _events(home):
    from core.event_log import TelemetryEventLog
    return TelemetryEventLog(os.path.join(home, "telemetry_events.db")).query(kind="owner_command")


def test_every_verb_is_one_event_without_arguments_or_body(home):
    ovl.record_owner_command("u1", "/status", "telegram", "Status:\n• all fine", data_dir=home)
    ovl.record_owner_command("u1", "/send 5 USDC to 0xabc", "telegram", "sent", data_dir=home)
    ovl.record_owner_command("u1", "/goals", "console", failed=True, data_dir=home)
    rows = [e["attrs"] for e in reversed(_events(home))]
    assert rows[0] == {"verb": "/status", "seat": "telegram", "outcome": "ok",
                       "reply_chars": len("Status:\n• all fine")}
    assert rows[1]["verb"] == "/send" and "0xabc" not in json.dumps(rows)
    assert rows[2]["outcome"] == "failed"
    assert ovl.last_command_ts("u1", "/status", data_dir=home) is not None
    assert ovl.last_command_ts("u1", "/goals", data_dir=home) is None      # it failed
    assert ovl.last_command_ts("u2", "/status", data_dir=home) is None


def test_the_last_status_text_is_kept_per_seat_and_overwritten(home):
    ovl.record_owner_command("u1", "/status", "telegram", "first", data_dir=home)
    ovl.record_owner_command("u1", "/status", "telegram", "second", data_dir=home)
    ovl.record_owner_command("u1", "/status", "console", "third", data_dir=home)
    got = ovl.last_status("u1", data_dir=home)
    assert got["telegram"]["text"] == "second" and got["console"]["text"] == "third"
    assert ovl.last_status("u2", data_dir=home) == {}
    with open(os.path.join(home, ovl.LAST_STATUS_FILE), "w") as fh:
        fh.write("{not json")
    with pytest.raises(ValueError):
        ovl.last_status("u1", data_dir=home)          # unreadable is not "none"


def test_the_telegram_dispatch_records_a_verb_that_writes_no_owner_thread_row(home, monkeypatch):
    from surfaces.telegram import card_ops
    result = SimpleNamespace(
        inbound=SimpleNamespace(identity=SimpleNamespace(user_id="u1", source=None), text="/status"),
        decision=SimpleNamespace(session_id="s1"))
    agent = SimpleNamespace(container=SimpleNamespace(config=SimpleNamespace(data_dir=home)))
    assert card_ops.record_out(agent, result, "/status", "Status: ok") == "Status: ok"
    card_ops.record_verb(agent, result, "/status", failed=True)
    assert [e["attrs"]["outcome"] for e in reversed(_events(home))] == ["ok", "failed"]
