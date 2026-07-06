import asyncio, os, tempfile
import pytest
from core.surfaces.outbound_allowlist import OutboundAllowlist
from core.surfaces.outbound_target import resolve_target_tier

# The action closure is thin; we test the decision+route contract via a small helper
# that Task 5 factors out: tools.controller.message_send.perform_message_send
from tools.controller.message_send import perform_message_send
from tools.controller.action_registration import _is_forged_or_autonomous_turn


class _FakeExecutionContext:
    def __init__(self, is_sub_agent=False, role="orchestrator", metadata=None,
                 session_id="s1"):
        self.is_sub_agent = is_sub_agent
        self.role = role
        self.metadata = metadata or {}
        self.session_id = session_id


class _FakeControllerSelf:
    """Stand-in for the Controller (`self`) the message closure passes in."""
    _is_sub_agent = False
    session_id = "s1"


def test_forged_turn_guard_blocks_sub_agent():
    ctx = _FakeExecutionContext(is_sub_agent=True)
    assert _is_forged_or_autonomous_turn(ctx, _FakeControllerSelf()) is True


def test_forged_turn_guard_blocks_self_wake_reentry():
    # SK-F10 shape: role='orchestrator', is_sub_agent=False (looks like a genuine
    # owner turn) but the drained turn_kind marks it as a forged self-wake re-entry.
    ctx = _FakeExecutionContext(is_sub_agent=False, role="orchestrator",
                                 metadata={"turn_kind": "self_wake"})
    assert _is_forged_or_autonomous_turn(ctx, _FakeControllerSelf()) is True


def test_forged_turn_guard_blocks_delegation_result_reentry():
    ctx = _FakeExecutionContext(is_sub_agent=False, role="orchestrator",
                                 metadata={"turn_kind": "delegation_result"})
    assert _is_forged_or_autonomous_turn(ctx, _FakeControllerSelf()) is True


def test_forged_turn_guard_allows_genuine_owner_turn():
    ctx = _FakeExecutionContext(is_sub_agent=False, role="orchestrator", metadata={})
    assert _is_forged_or_autonomous_turn(ctx, _FakeControllerSelf()) is False

class _Router:
    def __init__(self): self.sent = []
    async def send_message(self, chat_id, text, surface_id="telegram"):
        self.sent.append((surface_id, chat_id, text)); return True

def _al(tmp): return OutboundAllowlist(os.path.join(tmp, "a.db"))

def test_denied_does_not_send():
    tmp = tempfile.mkdtemp(); router = _Router()
    res = asyncio.run(perform_message_send(
        router=router, allowlist=_al(tmp), owner_targets={"telegram": "999"},
        user_id="rob", surface="telegram", target="555", text="hi", action="send"))
    assert res["success"] is False and res["tier"] == "denied" and router.sent == []

def test_owner_sends():
    tmp = tempfile.mkdtemp(); router = _Router()
    res = asyncio.run(perform_message_send(
        router=router, allowlist=_al(tmp), owner_targets={"telegram": "999"},
        user_id="rob", surface="telegram", target="999", text="hi", action="send"))
    assert res["success"] is True and res["tier"] == "owner" and router.sent[0] == ("telegram","999","hi")

def test_allowlisted_sends():
    tmp = tempfile.mkdtemp(); al = _al(tmp); al.allow("rob","telegram","555")
    router = _Router()
    res = asyncio.run(perform_message_send(
        router=router, allowlist=al, owner_targets={"telegram":"999"},
        user_id="rob", surface="telegram", target="555", text="hi", action="send"))
    assert res["success"] is True and res["tier"] == "allowlisted"
