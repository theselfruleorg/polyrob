"""WHY an X write needs the owner, in one line.

The approval gate's refusal and waiting text carry the real cause and the
owner's remedy, so the agent can repeat it truthfully. Never raises.
"""
from typing import Any


def owner_gap_reason(tool: Any, execution_context: Any) -> str:
    if execution_context is None:
        return ""
    try:
        from polyrob.sdk import (forged_kind, is_leaf, is_owner_tenant, is_room_turn,
                                 turn_tainted)
        if turn_tainted(execution_context):
            return "a correspondent's message (not the owner's) is in this session"
        if is_leaf(execution_context):
            return "a delegated sub-agent never posts as the owner"
        if forged_kind(execution_context):
            return "a self-wake / delegation re-entry is not the owner asking"
        if is_room_turn(execution_context):
            return "a room turn is not the owner asking"
        if not is_owner_tenant(execution_context):
            return "this turn is not the owner's"
        if tool._require_approval():
            return "TWITTER_REQUIRE_APPROVAL is on"
    except Exception:
        tool.logger.debug("twitter owner-gap reason failed", exc_info=True)
    return ""


def with_reason(tool: Any, text: str, execution_context: Any) -> str:
    reason = owner_gap_reason(tool, execution_context)
    return f"{text} Why it needs the owner: {reason}." if reason else text
