"""Regression tests for A2A task ownership enforcement (IDOR fix).

Before the fix, get_task/send_message performed a GLOBAL session lookup by
task_id with no owner check, so any authenticated tenant could read or write
another tenant's task. These tests drive the handler methods directly with a
non-owner user_id and assert a not-found error is raised BEFORE any read of
history/artifacts or any write/resume.
"""
import asyncio

import pytest

from api.a2a.task_handler import A2ATaskHandler
from api.a2a.models import A2AMessage


class _FakeAgent:
    def __init__(self, session_info):
        self._session_info = session_info
        self.submit_calls = []
        self.run_calls = []
        self.session_manager = None  # push-config: _get_session_manager() -> None (skip persist)

    async def get_session_by_id(self, task_id):
        return dict(self._session_info)

    async def run_session(self, user_id, task_id):  # pragma: no cover - must not run
        self.run_calls.append((user_id, task_id))


class _FakeContainer:
    def __init__(self, agent):
        self._agent = agent

    def get_agent(self, name):
        return self._agent

    def get_service(self, name):
        return None


def _handler(owner="owner-user", status="running"):
    agent = _FakeAgent({
        "user_id": owner,
        "status": status,
        "metadata": {},
        "config": {},
        "created_at": "", "updated_at": "",
    })
    return A2ATaskHandler(_FakeContainer(agent)), agent


def _msg(text="attacker payload"):
    return A2AMessage(role="user", parts=[{"kind": "text", "text": text}])


def test_get_task_rejects_non_owner():
    handler, _ = _handler(owner="owner-user")
    with pytest.raises(ValueError, match="not found"):
        asyncio.run(
            handler.get_task("victim-session", history_length=0, user_id="attacker")
        )


def test_get_task_allows_owner():
    handler, _ = _handler(owner="owner-user")
    task = asyncio.run(
        handler.get_task("owned-session", history_length=0, user_id="owner-user")
    )
    assert task.id == "owned-session"


def test_get_task_no_user_id_skips_check_for_internal_callers():
    # Internal re-fetch (user_id=None) must not enforce — used by send/cancel after auth.
    handler, _ = _handler(owner="owner-user")
    task = asyncio.run(
        handler.get_task("owned-session", history_length=0)
    )
    assert task.id == "owned-session"


def test_send_message_rejects_non_owner_before_write():
    handler, agent = _handler(owner="owner-user", status="completed")
    with pytest.raises(ValueError, match="not found"):
        asyncio.run(
            handler.send_message("victim-session", _msg(), user_id="attacker")
        )
    # The attacker must NOT have resumed/executed the victim's session.
    assert agent.run_calls == []


# ── push-notification-config ownership (S5 IDOR) ────────────────────────────

def _pnc(url="https://attacker.example/hook", token="t"):
    from api.a2a.models import PushNotificationConfig
    return PushNotificationConfig(url=url, token=token)


def test_set_push_config_rejects_non_owner_before_write():
    handler, agent = _handler(owner="owner-user")
    with pytest.raises(ValueError, match="not found"):
        asyncio.run(
            handler.set_push_notification_config("victim-session", _pnc(), user_id="attacker")
        )
    # Nothing was persisted for the victim task.
    assert "victim-session" not in handler._push_configs


def test_set_push_config_allows_owner():
    handler, _ = _handler(owner="owner-user")
    ok = asyncio.run(
        handler.set_push_notification_config("owned-session", _pnc(), user_id="owner-user")
    )
    assert ok is True
    assert "owned-session" in handler._push_configs


def test_get_push_config_rejects_non_owner():
    handler, _ = _handler(owner="owner-user")
    asyncio.run(handler.set_push_notification_config("owned-session", _pnc(), user_id=None))
    with pytest.raises(ValueError, match="not found"):
        asyncio.run(handler.get_push_notification_config("owned-session", user_id="attacker"))


def test_delete_push_config_rejects_non_owner():
    handler, _ = _handler(owner="owner-user")
    asyncio.run(handler.set_push_notification_config("owned-session", _pnc(), user_id=None))
    with pytest.raises(ValueError, match="not found"):
        asyncio.run(handler.delete_push_notification_config("owned-session", user_id="attacker"))
    # Still present — attacker could not delete it.
    assert "owned-session" in handler._push_configs
