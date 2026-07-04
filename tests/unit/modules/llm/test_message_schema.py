"""Tests for typed message origin (chat-schema upgrade).

System-injected content (loop interventions, approvals, memory) must be
distinguishable from a genuine user turn instead of being crammed into a plain
HumanMessage. We add a typed `origin` + a control-message factory that envelopes
the content so neither the model nor a human reader confuses it with user input.
"""

import pytest

from modules.llm.messages import (
    HumanMessage,
    MessageOrigin,
    make_control_message,
)


def test_human_message_defaults_to_user_origin():
    msg = HumanMessage(content="hello")
    assert msg.origin == MessageOrigin.USER


def test_control_message_carries_origin_and_is_user_role_on_wire():
    msg = make_control_message("Stop repeating actions.", MessageOrigin.INTERVENTION)
    assert isinstance(msg, HumanMessage)
    assert msg.origin == MessageOrigin.INTERVENTION
    # Still a user-role message on the wire (providers have no 'intervention' role)...
    assert msg.to_dict()["role"] == "user"
    # ...but the content is enveloped so it is clearly NOT a genuine user turn.
    assert "<system-directive>" in msg.content
    assert "Stop repeating actions." in msg.content


def test_control_message_envelope_tag_varies_by_origin():
    mem = make_control_message("found X", MessageOrigin.MEMORY)
    appr = make_control_message("approved", MessageOrigin.APPROVAL)
    assert "<session-memory>" in mem.content
    assert "<approval-result>" in appr.content


def test_origin_is_not_leaked_onto_the_wire():
    """`origin` is in-process metadata; it must not appear in the API dict."""
    msg = make_control_message("note", MessageOrigin.SYSTEM_NOTE)
    assert "origin" not in msg.to_dict()


def test_user_origin_message_is_not_enveloped():
    msg = make_control_message("real user text", MessageOrigin.USER)
    assert msg.content == "real user text"  # genuine user content stays clean
    assert msg.origin == MessageOrigin.USER
