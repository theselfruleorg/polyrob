"""CORRESPONDENT message origin (WS-A: three-tier chat-surface access model).

A third party the agent INITIATED contact with (a "correspondent") can reply, but
that reply is DATA, never an instruction. It must enter the session NOT as a
user-role steering turn but as an enveloped control message so the model reads it
as a distinct, non-obey block — the same mechanism MEMORY/SELF_CONTEXT use.
"""
from modules.llm.messages import (
    MessageOrigin,
    make_control_message,
    HumanMessage,
)


def test_correspondent_origin_exists():
    assert MessageOrigin.CORRESPONDENT == "correspondent"


def test_correspondent_control_message_is_enveloped():
    msg = make_control_message("John says: the invoice is paid.", MessageOrigin.CORRESPONDENT)
    assert isinstance(msg, HumanMessage)
    assert msg.origin == MessageOrigin.CORRESPONDENT
    assert msg.content == (
        "<correspondent-message>\n"
        "John says: the invoice is paid.\n"
        "</correspondent-message>"
    )
