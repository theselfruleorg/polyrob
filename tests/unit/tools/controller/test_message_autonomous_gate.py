"""Battle-test night-2 fix (2026-07-14): the `message` action's forged/autonomous
gate is owner-relaxable to ALLOWLISTED-ONLY sends via MESSAGE_AUTONOMOUS_ALLOWLISTED.

Default OFF = blanket refusal (byte-identical legacy). ON = the gate returns None
and the send proceeds to the normal target-tier gate (owner/allowlisted/denied),
so an autonomous goal can post to the owner-allowlisted promo chats but never to
an arbitrary target.
"""
from tools.controller.action_registration import _autonomous_message_refusal


class _Ctx:
    def __init__(self, is_sub_agent=False, role="orchestrator", metadata=None, session_id="s1"):
        self.is_sub_agent = is_sub_agent
        self.role = role
        self.metadata = metadata or {}
        self.session_id = session_id


class _Controller:
    _is_sub_agent = False
    session_id = "s1"


def test_flag_off_autonomous_turn_refused(monkeypatch):
    monkeypatch.delenv("MESSAGE_AUTONOMOUS_ALLOWLISTED", raising=False)
    res = _autonomous_message_refusal(_Ctx(is_sub_agent=True), _Controller())
    assert res is not None
    assert "not permitted" in res.extracted_content
    assert "MESSAGE_AUTONOMOUS_ALLOWLISTED" in res.extracted_content


def test_flag_on_autonomous_turn_falls_through_to_tier_gate(monkeypatch):
    monkeypatch.setenv("MESSAGE_AUTONOMOUS_ALLOWLISTED", "true")
    assert _autonomous_message_refusal(_Ctx(is_sub_agent=True), _Controller()) is None


def test_forged_turn_kind_respects_flag(monkeypatch):
    ctx = _Ctx(metadata={"turn_kind": "self_wake"})
    monkeypatch.delenv("MESSAGE_AUTONOMOUS_ALLOWLISTED", raising=False)
    assert _autonomous_message_refusal(ctx, _Controller()) is not None
    monkeypatch.setenv("MESSAGE_AUTONOMOUS_ALLOWLISTED", "true")
    assert _autonomous_message_refusal(ctx, _Controller()) is None


def test_genuine_owner_turn_never_gated(monkeypatch):
    monkeypatch.delenv("MESSAGE_AUTONOMOUS_ALLOWLISTED", raising=False)
    ctx = _Ctx(is_sub_agent=False, role="orchestrator", metadata={})
    assert _autonomous_message_refusal(ctx, _Controller()) is None


def test_constants_accessor_default_off(monkeypatch):
    from agents.task.constants import message_autonomous_allowlisted
    monkeypatch.delenv("MESSAGE_AUTONOMOUS_ALLOWLISTED", raising=False)
    assert message_autonomous_allowlisted() is False
    monkeypatch.setenv("MESSAGE_AUTONOMOUS_ALLOWLISTED", "true")
    assert message_autonomous_allowlisted() is True
