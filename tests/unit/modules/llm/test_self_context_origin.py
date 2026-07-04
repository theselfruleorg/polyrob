"""SELF_CONTEXT message origin (polyrob Phase C: SOUL/IDENTITY self-context).

A SOUL/identity doc is injected as a frozen foundation control message (like
skills), so it must have its own MessageOrigin + envelope tag distinguishing it
from a genuine user turn and from injected skills/memory.
"""
from modules.llm.messages import (
    MessageOrigin,
    make_control_message,
    HumanMessage,
)


def test_self_context_origin_exists():
    assert MessageOrigin.SELF_CONTEXT == "self_context"


def test_self_context_control_message_is_enveloped():
    msg = make_control_message("You are ROB.", MessageOrigin.SELF_CONTEXT)
    assert isinstance(msg, HumanMessage)
    assert msg.origin == MessageOrigin.SELF_CONTEXT
    assert msg.content == "<self-context>\nYou are ROB.\n</self-context>"
