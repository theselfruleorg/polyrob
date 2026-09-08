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
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    from core import autonomy_control as ac
    from core.event_log import TelemetryEventLog
    from core.surfaces.user_delivery import deliver_user_message
    ac.pause(str(tmp_path), scopes=("pings",), via="test")
    log = TelemetryEventLog(str(tmp_path / "t.db"))
    out = await deliver_user_message(None, "rob", "▶ goal started", source="self_evolution",
                                     event_log=log)
    assert out == "paused"
    notices = log.query(kind="owner_notice", user_id="rob")
    assert notices and "[held by owner pause; source=self_evolution]" in (notices[0]["attrs"].get("text") or "")
    deliveries = log.query(kind="user_delivery", user_id="rob")
    assert deliveries and deliveries[0]["attrs"]["outcome"] == "paused"
