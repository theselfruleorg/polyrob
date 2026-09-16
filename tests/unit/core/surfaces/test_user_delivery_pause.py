"""031 T7: the delivery rail holds lifecycle pings + escalations at ONE choke point."""
import pytest


@pytest.mark.asyncio
async def test_lifecycle_ping_is_held_while_paused(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    from core import autonomy_control as ac
    from core.surfaces.user_delivery import deliver_user_message
    ac.pause(str(tmp_path), via="test")
    out = await deliver_user_message(None, "rob", "▶ goal started", source="self_evolution",
                                     event_log=None)
    assert out == "paused"
    out = await deliver_user_message(None, "rob", "ask: need a key", source="goal_blocked",
                                     event_log=None)
    assert out == "paused"
    out2 = await deliver_user_message(None, "rob", "credits dead", source="credit_sentinel",
                                      event_log=None)
    assert out2 != "paused"


@pytest.mark.asyncio
async def test_held_ping_is_durably_recorded(tmp_path, monkeypatch):
    """A held LIFECYCLE ping is recorded on its attempt row, not in `/missed`.

    2026-09-15 (C3): `/missed` is the owner's recovery channel for content they
    still need; `▶ goal started` is ephemeral status and filled 822 of the 899
    rows there. The attempt row carries the full text, so nothing is dropped.
    """
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    from core import autonomy_control as ac
    from core.event_log import TelemetryEventLog
    from core.surfaces.user_delivery import deliver_user_message
    ac.pause(str(tmp_path), scopes=("pings",), via="test")
    log = TelemetryEventLog(str(tmp_path / "t.db"))
    out = await deliver_user_message(None, "rob", "▶ goal started", source="lifecycle",
                                     event_log=log)
    assert out == "paused"
    assert log.query(kind="owner_notice", user_id="rob") == []
    deliveries = log.query(kind="user_delivery", user_id="rob")
    assert deliveries and deliveries[0]["attrs"]["outcome"] == "paused"
    assert deliveries[0]["attrs"]["text"] == "▶ goal started"


@pytest.mark.asyncio
async def test_held_escalation_is_readable_in_missed(tmp_path, monkeypatch):
    """A non-lifecycle hold still writes the owner_notice `/missed` renders."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    from core import autonomy_control as ac
    from core.event_log import TelemetryEventLog
    from core.surfaces.user_delivery import deliver_user_message
    ac.pause(str(tmp_path), scopes=("pings",), via="test")
    log = TelemetryEventLog(str(tmp_path / "t.db"))
    out = await deliver_user_message(None, "rob", "ask: need an API key",
                                     source="goal_blocked", event_log=log)
    assert out == "paused"
    notices = log.query(kind="owner_notice", user_id="rob")
    assert notices and "ask: need an API key" in (notices[0]["attrs"].get("text") or "")
