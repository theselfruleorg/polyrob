"""T4-11 (2026-07-06 structural review): the A2A agent card advertises
``pushNotifications: True`` and ``send_push_notification`` was fully implemented
— but had ZERO callers, so a client that registered a webhook never heard back.

Wired at the task state transitions: run-settle (completed/failed — refetches
the terminal state) and cancel (pushes CANCELED directly). Config falls back to
the session-metadata mirror so a process restart doesn't orphan the webhook.
"""
import asyncio

import pytest

from api.a2a.models import A2ATaskState, PushNotificationConfig
from api.a2a.task_handler import A2ATaskHandler


class _FakeAgent:
    def __init__(self, session_info):
        self._session_info = session_info
        self.run_calls = []
        self.cancel_calls = []
        self.session_manager = None

    async def get_session_by_id(self, task_id):
        return dict(self._session_info)

    async def run_session(self, user_id, task_id):
        self.run_calls.append((user_id, task_id))

    async def cancel_session(self, user_id, session_id, force=False):
        self.cancel_calls.append(session_id)
        self._session_info["status"] = "cancelled"
        return True


class _FakeContainer:
    def __init__(self, agent):
        self._agent = agent

    def get_agent(self, name):
        return self._agent

    def get_service(self, name):
        return None


def _handler(status="running", metadata=None, owner="u1"):
    agent = _FakeAgent({
        "user_id": owner,
        "status": status,
        "metadata": metadata or {},
        "config": {},
        "created_at": "", "updated_at": "",
    })
    return A2ATaskHandler(_FakeContainer(agent)), agent


def test_get_push_config_memory_then_metadata_fallback():
    handler, _ = _handler()
    # memory hit
    handler._push_configs["t1"] = PushNotificationConfig(url="https://cb.example/hook")
    assert handler._get_push_config("t1").url == "https://cb.example/hook"
    # metadata fallback (restart-survivable)
    meta = {"a2a_push_url": "https://cb.example/hook2", "a2a_push_token": "tok"}
    cfg = handler._get_push_config("t2", {"metadata": meta})
    assert cfg is not None and cfg.url == "https://cb.example/hook2" and cfg.token == "tok"
    # neither
    assert handler._get_push_config("t3", {"metadata": {}}) is None


def test_cancel_task_pushes_canceled(monkeypatch):
    handler, agent = _handler(status="running")
    handler._push_configs["t1"] = PushNotificationConfig(url="https://cb.example/hook")
    sent = []

    async def fake_send(task_id, state, message=None):
        sent.append((task_id, state))
        return True

    monkeypatch.setattr(handler, "send_push_notification", fake_send)
    asyncio.new_event_loop().run_until_complete(handler.cancel_task("t1", "u1"))
    assert sent == [("t1", A2ATaskState.CANCELED)]


def test_run_settle_notifies_even_on_crash(monkeypatch):
    handler, agent = _handler(status="completed")
    notified = []

    async def fake_notify(task_id, message=None):
        notified.append(task_id)
        return True

    monkeypatch.setattr(handler, "_notify_push_state", fake_notify)

    async def boom(user_id, task_id):
        raise RuntimeError("agent crashed")

    agent.run_session = boom
    with pytest.raises(RuntimeError):
        asyncio.new_event_loop().run_until_complete(
            handler._run_session_with_push(agent, "u1", "t1"))
    assert notified == ["t1"]


def test_notify_push_state_maps_session_status(monkeypatch):
    handler, agent = _handler(status="completed")
    handler._push_configs["t1"] = PushNotificationConfig(url="https://cb.example/hook")
    sent = []

    async def fake_send(task_id, state, message=None):
        sent.append((task_id, state))
        return True

    monkeypatch.setattr(handler, "send_push_notification", fake_send)
    ok = asyncio.new_event_loop().run_until_complete(handler._notify_push_state("t1"))
    assert ok is True
    assert sent == [("t1", A2ATaskState.COMPLETED)]


def test_send_task_wires_the_push_wrapper():
    import inspect

    src = inspect.getsource(A2ATaskHandler)
    assert "_run_session_with_push" in src
    # both spawn points (initial run + resume) go through the wrapper
    assert src.count("_run_session_with_push") >= 3  # def + 2 call sites
