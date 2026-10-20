"""The one step-budget notice at 80 % (083 WS-5).

The state message shows ``Current step: N`` with no denominator, so the model
does not ration its work against a number it cannot change. Instead it gets ONE
notice, on the ephemeral rail, when the run reaches 80 % of its step cap:
save durable progress, then continue. The "one turn to report" sentence is only
said where the 083 WS-2 last-report turn really runs (``summary_turn_applies``).

Fail-open: any error leaves the step untouched.
"""
from __future__ import annotations

import logging
import math
from typing import Any

from modules.llm.messages import MessageOrigin, make_control_message

logger = logging.getLogger(__name__)

_MIN_STEPS = 4  # a cap of 4 or fewer gets no notice
_REPORT_SENTENCE = " At the limit you get one turn to report."


def budget_notice_text(used: int, max_steps: int, report_turn: bool) -> str:
    text = (
        f"You have used {used} of {max_steps} steps. Save durable progress now, "
        "then continue; do not stop because of this notice."
    )
    return text + (_REPORT_SENTENCE if report_turn else "")


def maybe_push_budget_notice(agent: Any, step_num: int, max_steps: int) -> bool:
    """Push the notice once per run, before step ``step_num + 1``. True when pushed."""
    try:
        if getattr(agent, "_budget_notice_sent", False) or max_steps <= _MIN_STEPS:
            return False
        if step_num + 1 < math.ceil(0.8 * max_steps):
            return False
        agent._budget_notice_sent = True
        from agents.task.agent.core.exhaustion_summary import summary_turn_applies
        text = budget_notice_text(step_num, max_steps, summary_turn_applies(agent))
        agent.message_manager.push_ephemeral_message(
            make_control_message(text, MessageOrigin.GUIDANCE))
        return True
    except Exception:
        logger.debug("budget notice: push failed (the step runs without it)", exc_info=True)
        return False
