import asyncio, types
from agents.task.task_agent_delivery import TaskAgentDeliveryMixin  # adjust to the real mixin name


class _Agent(TaskAgentDeliveryMixin):
    def __init__(self):
        self.container = None
        self.session_manager = types.SimpleNamespace(get_session_info=lambda sid: None)
        self.logger = None


def test_group_delivery_ignores_correspondent_flag(monkeypatch, caplog):
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "false")
    a = _Agent()
    # No session -> False, but the WARN proves we got PAST the flag gate.
    out = asyncio.run(a.deliver_correspondent_data("sid", "8123", "hi", group=True, surface="telegram"))
    assert out is False
    assert any("not resident/recreatable" in r.getMessage() for r in caplog.records)


def test_dm_correspondent_delivery_still_gated(monkeypatch, caplog):
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "false")
    a = _Agent()
    out = asyncio.run(a.deliver_correspondent_data("sid", "8123", "hi", surface="telegram"))
    assert out is False
    assert not any("not resident/recreatable" in r.getMessage() for r in caplog.records)
