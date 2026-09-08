"""031 T9: /pause /resume /halt on Telegram — thin over the one owner-admin API."""
import pytest

from core.surfaces.dispatcher import _COMMANDS
from surfaces.telegram.harness import _OWNER_ADMIN_COMMANDS, _pause_reply, help_commands


def test_pause_is_routable_and_helped():
    assert "/pause" in _COMMANDS and "/pause" in _OWNER_ADMIN_COMMANDS
    names = {c for c, _ in help_commands()}
    assert {"pause", "resume", "halt"} <= names


def test_pause_reply_writes_and_reports(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    from core.autonomy_control import read_state
    text = _pause_reply(str(tmp_path), "/pause", ["trading", "for", "2h"])
    assert text.startswith("⏸ Paused trading") and "120 min" in text
    assert read_state(str(tmp_path)).scopes == ("trading",)
    assert _pause_reply(str(tmp_path), "/halt", []).startswith("⏸ Paused everything")
    assert _pause_reply(str(tmp_path), "/resume", []).startswith("▶ Autonomy RESUMED")
    assert "unknown scope" in _pause_reply(str(tmp_path), "/pause", ["bogus"])
    _pause_reply(str(tmp_path), "/pause", ["cron", "streams"])
    assert "still paused: streams" in _pause_reply(str(tmp_path), "/resume", ["cron"])
