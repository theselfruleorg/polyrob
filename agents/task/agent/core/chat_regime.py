"""An owner chat is a conversation, not one task — coherency review 2026-10-13, E-A3.

A chat session is created with ``task=<its first message>``, and that message
was pinned, undated, as the authoritative task for the life of the chat.
``label_first_chat_message`` dates it and names it "the first message of this
chat" for a regime that does not pin its first message
(``agents.task.session_class.regime_policy``); cron, goal, planner and child
runs keep the authoritative task.

Skills are not matched per turn: an owner chat gets the short skill index and
loads what the turn needs with ``load_skill`` (081 Q2).
"""
from __future__ import annotations

from typing import Any


def label_first_chat_message(agent: Any) -> bool:
    """Relabel the pinned first message in an owner chat (:func:`first_message_is_not_the_task`). Fail-open."""
    try:
        if not first_message_is_not_the_task(agent):
            return False
        return bool(agent.message_manager.mark_initial_task_as_first_chat_message())
    except Exception as err:
        logger = getattr(agent, "logger", None)
        if logger is not None:
            logger.debug(f"first-message label skipped (non-fatal): {err}")
        return False


def first_message_is_not_the_task(agent: Any) -> bool:
    """The owner-chat predicate: the first message is not
    pinned AND a later owner turn wins. A room / correspondent session pins
    nothing either, but no owner turn wins there, so its first message is not
    relabelled "the latest owner message wins"."""
    from agents.task.session_class import regime_policy
    policy = regime_policy(agent)
    return (not policy.pin_initial_task) and policy.latest_owner_turn_wins

