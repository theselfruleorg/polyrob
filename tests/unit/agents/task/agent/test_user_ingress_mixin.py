"""P9 pass-5 — UserIngressMixin extracted from service.py."""
import logging
import types

import pytest

from agents.task.agent.core.user_ingress import UserIngressMixin


def test_agent_composes_user_ingress_mixin():
    from agents.task.agent.service import Agent
    assert issubclass(Agent, UserIngressMixin)
    for m in ("receive_user_message", "_drain_user_messages", "request_approval",
              "record_approval_decision", "_check_todo_completion", "_emit_todo_status"):
        assert getattr(Agent, m).__qualname__.startswith("UserIngressMixin")


class _Host(UserIngressMixin):
    def __init__(self):
        self.logger = logging.getLogger("ingress-test")
        self.agent_id = "a1"
        self.state = types.SimpleNamespace(n_steps=0)
        self.telemetry_manager = types.SimpleNamespace(capture_event=lambda e: None)


@pytest.mark.asyncio
async def test_receive_user_message_queues_via_hitl():
    h = _Host()
    queued = []

    class _HITL:
        async def queue_user_message(self, text, kind, metadata):
            queued.append((text, kind, metadata))

    h.hitl_manager = _HITL()
    await h.receive_user_message("hello", kind="comment")
    assert queued == [("hello", "comment", {})]


@pytest.mark.asyncio
async def test_request_approval_auto_approves():
    h = _Host()
    assert await h.request_approval("because", "checkpoint") is True


@pytest.mark.asyncio
async def test_drain_user_messages_stamps_delegation_result_delivered(tmp_path, monkeypatch):
    """T1.6 review fix (CRITICAL finding), wiring integration: the REAL
    _drain_user_messages call site — not just the underlying helper — stamps
    delivered_at the instant a delegation-result message is drained off the
    HITL queue, covering both the live async-delegation path (this test) and
    the cold-start sweep path (same kind/metadata shape via self_wake, see
    test_delegation_delivery_sweep.py)."""
    from agents.task.agent import autonomy_state
    from agents.task.agent.autonomy_state import AutonomyStateStore
    from core.security.forged_turns import DELEGATION_RESULT_KIND

    store = AutonomyStateStore(str(tmp_path / "autonomy_state.db"))
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0001",
                            goal="g", profile="p", parent_agent_id=None, dispatched_at=1.0)
    store.record_terminal("s1", "deleg_0001", status="completed",
                          completed_at=2.0, result_text="the child's output")
    monkeypatch.setattr(autonomy_state, "get_autonomy_state_store", lambda: store)

    class _WorkspaceContext:
        def get_workspace_changes(self, **kw):
            return types.SimpleNamespace(has_changes=lambda: False)

    class _HITL:
        async def drain_user_messages(self):
            return [{
                "text": "<delegation-result>...</delegation-result>",
                "kind": DELEGATION_RESULT_KIND,
                "metadata": {"source": "async_delegation", "delegation_id": "deleg_0001",
                             "status": "completed"},
            }]

    h = _Host()
    h.session_id = "s1"
    h.user_id = "u1"
    h.hitl_manager = _HITL()
    h.workspace_context = _WorkspaceContext()

    messages = await h._drain_user_messages()

    assert len(messages) == 1
    assert store.get("s1", "deleg_0001")["delivered_at"] is not None


@pytest.mark.asyncio
async def test_drain_user_messages_does_not_stamp_ordinary_comment(tmp_path, monkeypatch):
    """A genuine 'comment' message never triggers a stamp, even one that
    happens to carry a delegation_id-shaped metadata key (security scoping:
    metadata on ordinary messages is caller-supplied over the public HTTP
    API)."""
    from agents.task.agent import autonomy_state
    from agents.task.agent.autonomy_state import AutonomyStateStore

    store = AutonomyStateStore(str(tmp_path / "autonomy_state.db"))
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0001",
                            goal="g", profile="p", parent_agent_id=None, dispatched_at=1.0)
    store.record_terminal("s1", "deleg_0001", status="completed",
                          completed_at=2.0, result_text="r")
    monkeypatch.setattr(autonomy_state, "get_autonomy_state_store", lambda: store)

    class _WorkspaceContext:
        def get_workspace_changes(self, **kw):
            return types.SimpleNamespace(has_changes=lambda: False)

    class _HITL:
        async def drain_user_messages(self):
            return [{"text": "hi", "kind": "comment",
                     "metadata": {"delegation_id": "deleg_0001"}}]

    h = _Host()
    h.session_id = "s1"
    h.user_id = "u1"
    h.hitl_manager = _HITL()
    h.workspace_context = _WorkspaceContext()

    await h._drain_user_messages()

    assert store.get("s1", "deleg_0001")["delivered_at"] is None


class _WorkspaceContextNoChanges:
    def get_workspace_changes(self, **kw):
        return types.SimpleNamespace(has_changes=lambda: False)


@pytest.mark.asyncio
async def test_drain_sets_turn_reply_to_from_last_message():
    """044 T10 fix round 1 (finding #1, controller ruling): the room reply-anchor
    is recomputed from the LAST drained message's metadata on every non-empty
    drain — never a one-time direct poke that can go stale. Two drained
    batches with different anchors produce two different values."""
    h = _Host()
    h.session_id = "s1"
    h.user_id = "u1"
    h.orchestrator = types.SimpleNamespace()
    h.workspace_context = _WorkspaceContextNoChanges()

    class _HITL:
        def __init__(self, batch):
            self._batch = batch

        async def drain_user_messages(self):
            return self._batch

    h.hitl_manager = _HITL([
        {"text": "hi", "kind": "comment", "metadata": {"reply_to": "111"}},
    ])
    await h._drain_user_messages()
    assert h.orchestrator._turn_reply_to == "111"

    h.hitl_manager = _HITL([
        {"text": "hi again", "kind": "comment", "metadata": {"reply_to": "222"}},
    ])
    await h._drain_user_messages()
    assert h.orchestrator._turn_reply_to == "222"


@pytest.mark.asyncio
async def test_drain_clears_turn_reply_to_when_batch_has_none():
    """A drained batch with no reply_to (a plain STEER message, a self-wake, or
    an async-delegation reentry) clears any anchor left over from an earlier
    room trigger — otherwise an autonomous continuation would reuse a stale
    anchor (the second half of finding #1)."""
    h = _Host()
    h.session_id = "s1"
    h.user_id = "u1"
    h.orchestrator = types.SimpleNamespace(_turn_reply_to="stale")
    h.workspace_context = _WorkspaceContextNoChanges()

    class _HITL:
        async def drain_user_messages(self):
            return [{"text": "no anchor here", "kind": "comment", "metadata": {}}]

    h.hitl_manager = _HITL()
    await h._drain_user_messages()
    assert h.orchestrator._turn_reply_to is None


@pytest.mark.asyncio
async def test_drain_empty_batch_leaves_turn_reply_to_unchanged():
    """Mirrors _update_forged_turn_marker's own semantics: a mid-turn step with
    no new queued messages must not touch an anchor set by an earlier drain."""
    h = _Host()
    h.orchestrator = types.SimpleNamespace(_turn_reply_to="111")

    class _HITL:
        async def drain_user_messages(self):
            return []

    h.hitl_manager = _HITL()
    messages = await h._drain_user_messages()
    assert messages == []
    assert h.orchestrator._turn_reply_to == "111"
