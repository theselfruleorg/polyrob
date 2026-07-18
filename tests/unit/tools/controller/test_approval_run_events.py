"""019 P0 — approval wait-state feed events from the approval hook.

The hook emits ``awaiting_approval`` the moment the provider wait begins and
``approval_resolved`` (approved / denied / timeout) when it ends, so a blocked
run never renders as a silent stall. Emission is fail-open, flag-gated, and
skipped without a session_id (nowhere to route).
"""
import asyncio
from types import SimpleNamespace

import pytest

import tools.controller.approval as approval_mod
from agents.task.telemetry.views import ApprovalResolvedEvent, AwaitingApprovalEvent
from tools.controller.approval import (
    ApprovalProvider,
    AutoApprover,
    DenyByDefaultApprover,
    make_approval_hook,
)


@pytest.fixture
def captured(monkeypatch):
    events = []
    monkeypatch.setattr(
        "agents.task.telemetry.service.emit_feed_event", events.append
    )
    monkeypatch.delenv("RUN_EVENTS_ENABLED", raising=False)
    return events


def _ctx(session_id="sess1"):
    return SimpleNamespace(session_id=session_id)


@pytest.mark.asyncio
async def test_approved_emits_pair(captured):
    hook = make_approval_hook(AutoApprover(), {"run_code"})
    assert await hook("run_code", {}, _ctx()) is None
    assert [type(e) for e in captured] == [AwaitingApprovalEvent, ApprovalResolvedEvent]
    waiting, resolved = captured
    assert waiting.session_id == "sess1"
    assert waiting.action_name == "run_code"
    assert resolved.decision == "approved"
    assert resolved.waited_sec >= 0.0


@pytest.mark.asyncio
async def test_denied_emits_denied_decision(captured):
    hook = make_approval_hook(DenyByDefaultApprover(), {"run_code"})
    assert await hook("run_code", {}, _ctx()) is not None
    assert captured[-1].decision == "denied"


@pytest.mark.asyncio
async def test_timeout_emits_timeout_decision(captured):
    class _SlowApprover(ApprovalProvider):
        async def request(self, action_name, params, context):
            await asyncio.sleep(1.0)
            return True

    hook = make_approval_hook(_SlowApprover(), {"run_code"}, timeout=0.05)
    reason = await hook("run_code", {}, _ctx())
    assert reason and "timeout" in reason
    assert captured[-1].decision == "timeout"


@pytest.mark.asyncio
async def test_ungated_action_emits_nothing(captured):
    hook = make_approval_hook(DenyByDefaultApprover(), {"run_code"})
    assert await hook("read_file", {}, _ctx()) is None
    assert captured == []


@pytest.mark.asyncio
async def test_no_session_id_emits_nothing(captured):
    hook = make_approval_hook(AutoApprover(), {"run_code"})
    assert await hook("run_code", {}, None) is None
    assert captured == []


@pytest.mark.asyncio
async def test_flag_off_emits_nothing(captured, monkeypatch):
    monkeypatch.setenv("RUN_EVENTS_ENABLED", "off")
    hook = make_approval_hook(AutoApprover(), {"run_code"})
    assert await hook("run_code", {}, _ctx()) is None
    assert captured == []


@pytest.mark.asyncio
async def test_emit_failure_never_breaks_the_hook(monkeypatch):
    def _boom(_event):
        raise RuntimeError("emitter down")

    monkeypatch.setattr("agents.task.telemetry.service.emit_feed_event", _boom)
    monkeypatch.delenv("RUN_EVENTS_ENABLED", raising=False)
    hook = make_approval_hook(AutoApprover(), {"run_code"})
    assert await hook("run_code", {}, _ctx()) is None  # still allows
