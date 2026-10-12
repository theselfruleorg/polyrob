import asyncio, types

import agents.task.task_agent_delivery as delivery_mod
from agents.task.task_agent_delivery import TaskAgentDeliveryMixin  # adjust to the real mixin name


class _Agent(TaskAgentDeliveryMixin):
    def __init__(self):
        self.container = None
        self.session_manager = types.SimpleNamespace(get_session_info=lambda sid: None)
        self.logger = None


class _RecordingLogger:
    """Records every log call. The module logger is swapped for this, so the
    assertion does not depend on the process-wide logging state: on a CI xdist
    worker an earlier test left the shared ``agents.task_agent_lite`` logger
    unreachable for caplog (level/propagate/logging.disable), and the WARN this
    test looks for never reached caplog.records."""

    def __init__(self):
        self.messages = []

    def __getattr__(self, level):
        def _log(msg, *args, **_kw):
            self.messages.append(str(msg) % args if args else str(msg))
        return _log


def _run(monkeypatch, **kw):
    rec = _RecordingLogger()
    monkeypatch.setattr(delivery_mod, "logger", rec)
    out = asyncio.run(_Agent().deliver_correspondent_data("sid", "8123", "hi", surface="telegram", **kw))
    return out, rec.messages


def test_group_delivery_ignores_correspondent_flag(monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "false")
    # No session -> False, but the WARN proves we got PAST the flag gate.
    out, messages = _run(monkeypatch, group=True)
    assert out is False
    assert any("not resident/recreatable" in m for m in messages), messages


def test_dm_correspondent_delivery_still_gated(monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "false")
    out, messages = _run(monkeypatch)
    assert out is False
    assert not any("not resident/recreatable" in m for m in messages), messages
