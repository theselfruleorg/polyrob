"""Pure builders for goal-run prompts and outcome parsing (unit-testable, no I/O)."""
from __future__ import annotations

from typing import Optional

from agents.task.goals.board import Goal

_OUTCOME_INSTRUCTION = (
    "When you are finished, end your final message with ONE line:\n"
    "OUTCOME: <the concrete ids/paths/urls you produced, or NONE plus why>"
)


def build_goal_run_task(goal: Goal, objective: Optional[Goal]) -> str:
    parts = []
    if objective is not None:
        parts.append(
            "STANDING OBJECTIVE (all work must advance it):\n"
            f"{objective.title}\n{objective.body}".rstrip())
    else:
        parts.append("This is a one-off goal (no standing objective attached).")
    parts.append(f"GOAL: {goal.title}\n{goal.body or ''}".rstrip())
    acceptance = (goal.payload or {}).get("acceptance")
    if acceptance:
        parts.append(f"Definition of done (acceptance): {acceptance}")
    parts.append(_OUTCOME_INSTRUCTION)
    return "\n\n".join(parts)


def extract_outcome_line(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    for line in reversed(text.strip().splitlines()):
        s = line.strip()
        if s.upper().startswith("OUTCOME:"):
            out = s[len("OUTCOME:"):].strip()
            return out or None
    return None
