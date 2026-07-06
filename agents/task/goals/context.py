"""Pure builders for goal-run prompts and outcome parsing (unit-testable, no I/O)."""
from __future__ import annotations

import re
from typing import Optional

from agents.task.goals.board import Goal

_OUTCOME_INSTRUCTION = (
    "When you are finished, end your final message with ONE line:\n"
    "OUTCOME: <the concrete ids/paths/urls you produced, or NONE plus why>\n"
    "If the acceptance requires an external action you cannot or should not execute "
    "(a disabled capability, a missing credential, or something needing owner approval), "
    "do NOT report success — end instead with ONE line:\n"
    "OUTCOME: BLOCKED — <exactly what you need to proceed>"
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


_BLOCKED_RE = re.compile(r"^blocked\b[\s—–:\-]*", re.IGNORECASE)


def parse_blocked_outcome(outcome: Optional[str]) -> Optional[str]:
    """If an extracted OUTCOME is a BLOCKED declaration, return the stated need.

    Accepts em-dash/en-dash/hyphen/colon/whitespace separators and any case; the
    declaration must LEAD the outcome ('blocked' mentioned mid-sentence is prose,
    not a declaration). Returns None for a non-BLOCKED outcome, '' for a bare
    'BLOCKED' with no stated need.
    """
    if not outcome:
        return None
    m = _BLOCKED_RE.match(outcome.strip())
    if not m:
        return None
    return outcome.strip()[m.end():].strip()
