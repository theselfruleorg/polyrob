"""The last report at the step limit (083 WS-2), at a loop stop, and at the
dollar cap (095 W1d).

When the run loop runs out of steps (the ``for … else`` in ``run_loop.py``) or
the loop breaker ends the run (``StopReason.LOOP``, ``LOOP_SUMMARY_REQUEST``),
the agent gets ONE extra model call to report what it finished, what remains
and what is blocked. The reply is recorded as the agent's words and nothing in
it runs — not even ``done``: an exhausted run is never a board success (T2-01).

At the dollar cap the call is bounded by ``BUDGET_REPORT_MAX_TOKENS`` output
tokens; its cost is billed like any call and named in the halt text.

The call keeps the SAME tools: a tools-free call breaks the Anthropic history
rule for tool_use turns and drops the cached prefix
(``messages/compactor.py``). The request rides the ephemeral rail, so it is
never left in history.

Scope: autonomous runs (goal / cron), sub-agents, and an owner chat turn that
published nothing (prod 2026-10-28: a turn enrolled the agent as a merchant,
hit the cap and said nothing — the premise that "a chat turn already replies
through ``send_message``" did not hold). The owner-turn end notice carries the
report (``run_outcome.session_turn_end_notice``). A room or a correspondent
session never gets the call.
Fail-open everywhere: any error leaves today's behaviour. ``CancelledError``
re-raises.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Optional

from modules.llm.messages import MessageOrigin, make_control_message

logger = logging.getLogger(__name__)

EXHAUSTION_SUMMARY_REQUEST = (
    "The step limit is reached; no more tools will run. Report what you finished "
    "(with evidence), what remains and the next step, and what is blocked. Put the "
    "report in the `done` text. Start no new work."
)
#: The same last report when the loop breaker ended the run (StopReason.LOOP).
LOOP_SUMMARY_REQUEST = (
    "This run is stopped because you repeated the same tool call with the same result; "
    "no more tools will run. Report what you finished (with evidence), what remains and "
    "the next step, and what blocked you. Put the report in the `done` text. Start no "
    "new work."
)
#: The same last report when a dollar cap ended the run (StopReason.RUN_BUDGET).
BUDGET_SUMMARY_REQUEST = (
    "The cost budget of this run is reached; no more tools will run. Report what you "
    "finished (with evidence), what remains and the next step, and what is blocked. Put "
    "the report in the `done` text, briefly. Start no new work."
)
#: Fixed output bound on the dollar-cap report call (no flag).
BUDGET_REPORT_MAX_TOKENS = 1500
SUMMARY_TIMEOUT_S = 90
_MAX_CHARS = 4000


def summary_turn_applies(agent: Any) -> bool:
    """True for a sub-agent, an autonomous (goal / cron) session, or an owner
    chat turn that has published no reply."""
    if getattr(agent, "_is_sub_agent", False) is True:
        return True
    try:
        from agents.task.session_class import OWNER_CHAT, budget_class_autonomous, session_regime
        if budget_class_autonomous(getattr(agent, "session_id", None)):
            return True
        if session_regime(agent) != OWNER_CHAT:
            return False
        from core.surfaces.turn_reply import spoke_this_turn
        return not spoke_this_turn(getattr(agent, "orchestrator", None))
    except Exception:
        return False


def _params(action: Any) -> tuple:
    try:
        dump = action.model_dump(exclude_unset=True)
    except Exception:
        return "", {}
    for name, params in (dump or {}).items():
        if name != "interacted_element":
            return str(name), (params if isinstance(params, dict) else {})
    return "", {}


def summary_text(model_output: Any) -> str:
    """The report: the first ``done`` text, else the first ``send_message`` text,
    else the brain ``memory``. '' when none."""
    actions = list(getattr(model_output, "action", None) or [])
    for wanted in ("done", "send_message"):
        for action in actions:
            name, params = _params(action)
            if name == wanted or name.endswith("_" + wanted):
                text = str(params.get("text") or params.get("message") or "").strip()
                if text:
                    return text[:_MAX_CHARS]
    memory = getattr(getattr(model_output, "current_state", None), "memory", "") or ""
    return str(memory).strip()[:_MAX_CHARS]


def _emit(agent: Any, kind: str, attrs: dict) -> None:
    try:
        from core.event_log import emit
        emit(kind, source="run_loop", user_id=str(getattr(agent, "user_id", "") or ""),
             session_id=str(getattr(agent, "session_id", "") or ""), attrs=attrs)
    except Exception:
        pass


def _cleanup_call(agent: Any, mm: Any, request: Any) -> None:
    """Drop the request (consumed or not) and the unexecuted tool calls."""
    try:
        mm.commit_ephemeral_consumption()
        queue = getattr(mm, "_ephemeral_messages", None)
        if isinstance(queue, list) and request in queue:
            queue.remove(request)
    except Exception:
        pass
    try:
        tracker = getattr(agent, "tool_call_tracker", None)
        if tracker is not None:
            tracker.complete_step()
    except Exception:
        pass
    try:
        if hasattr(agent, "_current_normalized_tool_calls"):
            delattr(agent, "_current_normalized_tool_calls")
    except Exception:
        pass


async def run_budget_stop_report(agent: Any, max_steps: int, halt: str) -> str:
    """095 W1d: the one last report at the dollar cap. Returns *halt* with the
    report's own cost appended when it was measured."""
    try:
        from agents.task.agent.core.run_budget import _session_spend
        before = await _session_spend(agent)
        text = await run_exhaustion_summary(agent, max_steps, request=BUDGET_SUMMARY_REQUEST,
                                            budget_stop=True)
        if text is None or before is None:
            return halt
        after = await _session_spend(agent)
        if after is None:
            return halt
        cost = max(0.0, after - before)
        _emit(agent, "exhaustion_summary_cost", {"report_cost_usd": round(cost, 6)})
        return f"{halt}; the last report cost ${cost:.4f}"
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("dollar-cap last report: cost not measured", exc_info=True)
        return halt


async def run_exhaustion_summary(agent: Any, max_steps: int,
                                 request: str = EXHAUSTION_SUMMARY_REQUEST,
                                 *, budget_stop: bool = False) -> Optional[str]:
    """One extra call at the step limit (or at a loop stop, with *request* =
    ``LOOP_SUMMARY_REQUEST``); returns the recorded report or None.

    ``budget_stop``: the dollar cap ended the run — the budget guard is skipped
    (the cap is the reason for the call) and the output is bounded."""
    try:
        if not summary_turn_applies(agent):
            return None
        state = getattr(agent, "state", None)
        if getattr(agent, "_cancelled", False) or getattr(state, "stopped", False):
            return None
        from agents.task.agent.core.session_control import control_checkpoint
        if not await control_checkpoint(agent):
            return None
        from agents.task.agent.core.run_budget import check_run_budget
        if not budget_stop and await check_run_budget(agent) is not None:
            return None
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("exhaustion summary: guard probe failed", exc_info=True)
        return None

    mm = getattr(agent, "message_manager", None)
    request = make_control_message(request, MessageOrigin.INTERVENTION)
    try:
        mm.push_ephemeral_message(request)
        try:
            # Per call (a context variable), never a stamp on the shared client.
            from modules.llm.call_scope import bounded_output
            bound = (bounded_output(BUDGET_REPORT_MAX_TOKENS)
                     if budget_stop else contextlib.nullcontext())
            with bound:
                model_output = await asyncio.wait_for(
                    agent.get_next_action(mm.get_messages()), SUMMARY_TIMEOUT_S)
        finally:
            _cleanup_call(agent, mm, request)
        text = summary_text(model_output)
        if not text:
            _emit(agent, "exhaustion_summary_failed", {"error": "empty reply",
                                                       "max_steps": max_steps})
            return None
        # Record it. Nothing in the reply executes — done included.
        agent._last_result = agent.append_terminal_summary_item(text)
        state.exhausted_summary = text
        try:
            from agents.task.runtime.stop_reason import label_last_report
            mm.add_plan(label_last_report(text, _MAX_CHARS))
        except Exception:
            logger.debug("exhaustion summary: history note failed", exc_info=True)
        _emit(agent, "exhaustion_summary", {"chars": len(text), "max_steps": max_steps})
        return text
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("exhaustion summary failed (the run ends without a last report): %s", e)
        _emit(agent, "exhaustion_summary_failed", {"error": str(e)[:300],
                                                   "max_steps": max_steps})
        return None
