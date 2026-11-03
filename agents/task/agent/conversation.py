"""First-class multi-turn conversation over the existing agent loop (R4).

A Conversation owns ordered Turns and drives the agent one turn at a time with a
small per-turn step budget, returning the answer synchronously — instead of the
HITL resume FSM (which re-enters a 100-step task loop per reply). Server task
sessions are unaffected; this is opt-in via Conversation.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional


def extract_answer(history: Any) -> str:
    """The agent's reply for a turn = the last is_done result's extracted_content.

    Accepts either a flat iterable of ActionResults or the AgentHistoryList that
    Agent.run() actually returns. AgentHistoryList is a pydantic model: iterating
    it yields (field, value) tuples, not results, so unwrap via action_results()
    when available before scanning for the terminal is_done result.
    """
    results: Iterable[Any]
    if hasattr(history, "action_results") and callable(history.action_results):
        results = history.action_results()
    else:
        results = history
    answer = ""
    for result in results:
        if getattr(result, "is_done", False) and getattr(result, "extracted_content", None):
            answer = result.extracted_content
    return answer


# A REPL turn is an owner turn: the one interactive budget (REPL ⊇ Telegram).
from core.config_policy.goal_flags import interactive_max_steps  # noqa: E402


def _reset_turn_state(agent: Any, *, owner_line: bool = False) -> None:
    """Clear the cancel flag so a NEW user turn gets a fresh attempt.

    098 AG-01: the run-scoped halt (stopped, consecutive failures, failed
    providers, the LLM breaker) is cleared by ``run()`` itself
    (``AgentState.begin_run``), so a halted turn never wedges the REPL.

    093 E2: ``owner_line`` — the owner typed this line (a REPL line), so the retry
    wall starts empty, as it does for a drained owner batch
    (``user_ingress._drain_user_messages``). A REPL line never rides that queue.
    """
    if getattr(agent, "_cancelled", False):
        agent._cancelled = False
    if owner_line:
        agent._retry_wall_steps = 0
        try:
            agent.controller.reset_retry_wall()
        except Exception:
            pass


@dataclass
class Turn:
    user: str
    assistant: str = ""


@dataclass
class Conversation:
    agent: Any
    turns: List[Turn] = field(default_factory=list)

    async def respond(self, text: str, max_steps: Optional[int] = None, *,
                      typed_text: Optional[str] = None) -> str:
        """Run exactly one conversational turn and return the agent's reply.

        After the first turn we pass _continue_session=True so run() skips the
        once-per-session preamble (session-start telemetry + H-MEM create/load),
        which would otherwise re-fire on every REPL line.

        085 W3 (A1): ``typed_text`` is the line the owner TYPED, before any
        @-reference expansion put file content into ``text`` (default: ``text``).
        A REPL line never rides the HITL queue, so this is where the turn's owner
        words are set — otherwise the last drained batch (a /steer) stays the
        owner's words for every later line.
        """
        _reset_turn_state(self.agent, owner_line=True)  # recover; a fresh retry wall
        _mark_owner_turn(self.agent)                 # 098 AG-06: a REPL line is the owner
        _set_owner_turn_text(self.agent, text if typed_text is None else typed_text)
        _record_repl_line(self.agent, text)          # 061: the terminal is an owner seat
        self.agent.set_turn_input(text)              # append-as-turn (Task 7)
        history = await self.agent.run(
            max_steps=max_steps, _continue_session=bool(self.turns))
        answer = extract_answer(history)
        self.turns.append(Turn(user=text, assistant=answer))
        return answer


def _mark_owner_turn(agent: Any) -> None:
    from agents.task.agent.core.user_ingress import mark_owner_turn
    mark_owner_turn(getattr(agent, "orchestrator", None))


def _set_owner_turn_text(agent: Any, typed: Any) -> None:
    """The REPL line's typed words as the turn's owner text (085 W3). Fail-open."""
    orch = getattr(agent, "orchestrator", None)
    if orch is not None:
        try:
            orch._owner_turn_text = typed if isinstance(typed, str) else ""
        except Exception:
            pass


def _record_repl_line(agent: Any, text: str) -> None:
    """061: an owner's REPL line joins the ONE owner thread (via ``repl``).
    The reply is recorded by the turn latch. Fail-open."""
    try:
        from core.surfaces.owner_thread import record_owner_in
        record_owner_in(getattr(agent, "container", None),
                        str(getattr(agent, "user_id", "") or ""), text, via="repl",
                        session_id=str(getattr(agent, "session_id", "") or ""))
    except Exception:
        pass
