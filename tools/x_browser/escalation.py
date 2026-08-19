"""Owner escalation for the X signup obstacle taxonomy (Task 12).

Bridges a :class:`SignupPaused` to the owner via the existing rails — the durable
goal-board ask (``polyrob owner asks``/``fulfill``) and the one-rail owner notice
(:func:`core.surfaces.user_delivery.deliver_user_message`). No new value channel
is invented: the ask/fulfill rail is boolean today (design doc's documented
limitation), so a VALUE/PHONE ask CLEARS when the owner fulfills it (they handle
the step in the browser or out of band) and otherwise stays PAUSED. An
INTERACTIVE_CHALLENGE with a live headed driver polls ``challenge_cleared``;
headless, it pauses with the exact resume command.

Nothing here solves a CAPTCHA.
"""
from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Any, Optional

from core.surfaces.user_delivery import deliver_user_message
from tools.x_browser.signup import Obstacle, SignupPaused

logger = logging.getLogger(__name__)

_RESUME_CMD = "polyrob x-account signup --resume"


class EscalationOutcome(Enum):
    CLEARED = "cleared"   # the obstacle was resolved; the flow may continue
    PAUSED = "paused"     # owner must act later; the flow stays parked
    ABORTED = "aborted"   # terminal; do not retry


async def _notify(container, user_id, session_id, text) -> None:
    try:
        await deliver_user_message(container, user_id, text,
                                   source="agent", session_id=session_id,
                                   priority="critical")
    except Exception as e:
        logger.debug("x escalation notice failed: %s", e)


def _board_for(container, board):
    if board is not None:
        return board
    try:
        return container.get_service("goal_board") if container else None
    except Exception:
        return None


async def _wait_ask_fulfilled(board, user_id: str, ask_id: str,
                              timeout_sec: float, poll_interval: float) -> bool:
    """Poll until the ask leaves the OPEN set (owner fulfilled/decided it)."""
    from agents.task.goals.board import ASK_OPEN
    waited = 0.0
    while waited < timeout_sec:
        try:
            open_ids = {a.id for a in board.asks(user_id=user_id, status=ASK_OPEN)}
        except Exception as e:
            logger.debug("x escalation ask poll failed: %s", e)
            open_ids = {ask_id}
        if ask_id not in open_ids:
            return True
        await asyncio.sleep(poll_interval)
        waited += poll_interval
    return False


async def escalate_and_wait(
    container: Any,
    user_id: str,
    session_id: str,
    paused: SignupPaused,
    *,
    timeout_sec: float,
    board: Any = None,
    driver: Any = None,
    poll_interval: float = 2.0,
) -> EscalationOutcome:
    """Notify the owner about *paused* and wait (bounded) for it to clear."""
    obstacle = paused.obstacle
    board = _board_for(container, board)

    if obstacle is Obstacle.BLOCKED:
        await _notify(container, user_id, session_id,
                      f"X signup blocked and will not retry: {paused.prompt}")
        return EscalationOutcome.ABORTED

    if obstacle is Obstacle.INTERACTIVE_CHALLENGE:
        if driver is not None:
            await _notify(container, user_id, session_id,
                          "X is showing a challenge during signup — please solve "
                          "it in the open browser window; I'll continue once it "
                          "clears.")
            waited = 0.0
            while waited < timeout_sec:
                try:
                    if await driver.challenge_cleared():
                        return EscalationOutcome.CLEARED
                except Exception as e:
                    logger.debug("x challenge probe failed: %s", e)
                await asyncio.sleep(poll_interval)
                waited += poll_interval
            return EscalationOutcome.PAUSED
        # Headless: cannot show the challenge to the owner here.
        await _notify(container, user_id, session_id,
                      "X signup hit a visual challenge and this host has no "
                      f"display. Resume it locally: `{_RESUME_CMD}`.")
        return EscalationOutcome.PAUSED

    # VALUE_NEEDED / PHONE_REQUIRED — durable ask + notice, wait for fulfillment.
    if board is not None:
        try:
            ask = board.create_ask(
                user_id=user_id,
                what=f"X signup needs your input: {paused.prompt}",
                why="Answer with `polyrob owner fulfill <id>` after handling it.")
            ask_id = getattr(ask, "id", None)
        except Exception as e:
            logger.debug("x escalation create_ask failed: %s", e)
            ask_id = None
    else:
        ask_id = None

    await _notify(container, user_id, session_id,
                  f"X signup needs you: {paused.prompt} "
                  "(see `polyrob owner asks`).")

    if board is not None and ask_id is not None:
        if await _wait_ask_fulfilled(board, user_id, ask_id, timeout_sec, poll_interval):
            return EscalationOutcome.CLEARED
    return EscalationOutcome.PAUSED
