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


@pytest.mark.parametrize("surface,want", [("webview", "console"), ("telegram", "telegram"),
                                          ("discord", "discord")])
def test_pause_is_audited_with_the_seat_it_came_from(tmp_path, monkeypatch, surface, want):
    """CLI14 (audit 2026-10-03): the console shares this handler, and its
    pause/resume was audited as `via="telegram"`."""
    import asyncio

    from core.surfaces.dispatcher import RouteDecision, RouteKind
    from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
    from surfaces.telegram.harness import _handle_owner_admin
    from surfaces.telegram.inbound import InboundResult
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "alice")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)

    class _C:
        config = type("Cfg", (), {"data_dir": str(tmp_path)})()

        def get_service(self, n):
            return None

    agent = type("A", (), {"container": _C()})()
    src = SessionSource(surface, "555", "dm")
    result = InboundResult(
        inbound=InboundMessage(text="/pause trading",
                               identity=Identity(user_id="alice", source=src)),
        decision=RouteDecision(RouteKind.COMMAND, "k", command="/pause"))
    asyncio.run(_handle_owner_admin(agent, result, "/pause"))
    from core.autonomy_control import read_state
    assert read_state(str(tmp_path)).via == want
