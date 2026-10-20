"""Tool-availability transparency (proposal 013 §2.8, owner directive 2026-07-15).

The agent must never GUESS why a capability is missing. Every known tool that is not
loaded in the current session is disclosed with its gate and the remedy, in three tiers:

    loadable — registered in this deployment; just not in this session's tool_ids.
    disabled — exists but a config flag gates it off; the OWNER can enable it.
    reserved — owner-only capability (money-spend / trading / host): NEVER self-serve,
               and NEVER a valid reason to declare unrelated work 'blocked'.

Injected as a stable block in the system prompt (prompts.py) and reused by the goal
planner's grounding. Gated by TOOL_AVAILABILITY_HINT (default ON, both modes — pure
transparency, no capability change). Fail-open: any error yields "".
"""
from __future__ import annotations

import logging

from core.tool_capabilities import TOOL_CAPABILITIES, gate_disclosure_map
from core.config_policy.members import set_line
from core.tool_gates import remedy_text

logger = logging.getLogger(__name__)

# tool_id -> (gate description, tier, remedy shown to the agent). A VIEW (067 P1) of
# the ``gate`` field of the per-tool rows in core/tool_capabilities.py — declare a
# tool's disclosure there. Its membership differs from TOOL_GATE_FLAGS on purpose
# (see the note above gate_flag_map there).
GATED_TOOL_REGISTRY: dict = gate_disclosure_map()


def _hint_enabled() -> bool:
    from core.config_policy import _bool_env
    return _bool_env("TOOL_AVAILABILITY_HINT", True)


#: The compute tools whose gate has TWO limbs (a flag AND the posture). Their
#: registry row is a static fallback; the live line comes from ``compute_gate``.
_COMPUTE_TOOLS = {
    "code_execution": ("CODE_EXEC_ENABLED", "code_exec_enabled"),
    "shell": ("SHELL_TOOLS_ENABLED", "shell_tools_enabled"),
}

def _compute_limbs() -> dict:
    """Live state of every compute-gate limb (frozen-at-import posture + flags)."""
    from core.config_policy import capability_toggles as ct
    from core.config_policy.compute_posture import compute_posture
    return {
        "posture": compute_posture(),
        "code_exec_enabled": ct.code_exec_enabled(),
        "shell_tools_enabled": ct.shell_tools_enabled(),
    }


def compute_gate(tool: str) -> tuple:
    """(gate, tier, remedy) for a compute tool, naming the limb that is ACTUALLY unmet.

    Rob's self-review 2026-09-19 #2: prod ran ``AGENT_COMPUTE_POSTURE=1`` with
    ``CODE_EXEC_ENABLED=false`` and the note still said "owner raises
    AGENT_COMPUTE_POSTURE" — a remedy the deploy already satisfied, so the owner could
    not act on it. A disabled tool whose named remedy is already met must name the
    real blocker. Fail-open to the static registry row on any error.
    """
    static = GATED_TOOL_REGISTRY[tool]
    try:
        flag_name, limb = _COMPUTE_TOOLS[tool]
        limbs = _compute_limbs()
        posture = limbs["posture"]
        if posture < 1:
            return (f"AGENT_COMPUTE_POSTURE={posture} (needs AGENT_COMPUTE_POSTURE>=1)",
                    "disabled",
                    remedy_text("compute_posture"))
        if not limbs[limb]:
            return (f"{flag_name}=false (posture {posture} already met)",
                    "disabled",
                    remedy_text("compute_flag", flag=flag_name,
                                set_flag=set_line(flag_name, "true")))
        return (f"{flag_name}=true, posture {posture}", "loadable",
                remedy_text("compute_session"))
    except Exception:
        logger.debug("compute gate for %s failed (fail-open to registry)", tool, exc_info=True)
        return static


def grantable_autonomous_tools() -> list:
    """Tools a queued goal may be granted right now (planner grounding).

    Single source of truth: delegates to T3's ``tools.goal_tools.allowed_self_goal_tools()``
    (already mode-aware — expands under effective AUTONOMY_MODE=autonomous). No mode
    branch duplicated here.
    """
    try:
        from tools.goal_tools import allowed_self_goal_tools
        return sorted(allowed_self_goal_tools())
    except Exception:
        return []


def build_tool_availability_note(loaded_tool_ids, *, can_create_goals: bool = True,
                                 catalog_pinned: bool = False) -> str:
    """The <tool-availability> system-prompt block. '' when disabled or on any error.

    ``can_create_goals`` False (a leaf, a room, a correspondent session) drops the
    "Goals you create carry their OWN tools" line: that session creates no goal.
    ``catalog_pinned`` (081 bar 2): the <tool-catalog> owns every tool's status
    (its lines carry these gates — ``tools/tool_disclosure.py::_gate_line``), so
    the block keeps only the goal line, or is empty."""
    if not _hint_enabled():
        return ""
    if catalog_pinned:
        if not can_create_goals:
            return ""
        try:
            return ("<tool-availability>\nGoals you create carry their OWN tools: "
                    + ", ".join(sorted(grantable_autonomous_tools())) + ".\n</tool-availability>")
        except Exception:
            return ""
    try:
        loaded = set(loaded_tool_ids or ())
        lines = ["<tool-availability>",
                 "Tools NOT in this session, with the reason and remedy — a missing tool is "
                 "NEVER a blocker you invent workarounds or excuses for: name the gap, use the "
                 "remedy, or raise ONE ask to the owner."]
        grantable = set(grantable_autonomous_tools())
        rows: dict = {}   # line key -> tools
        for tool, (gate, tier, remedy) in sorted(GATED_TOOL_REGISTRY.items()):
            if tool in loaded:
                continue
            if tool in _COMPUTE_TOOLS:
                gate, tier, remedy = compute_gate(tool)
            elif (tier == "reserved" and tool in grantable
                  and "money" in TOOL_CAPABILITIES.get(tool, ())):
                # The same predicate tools/tool_disclosure.py uses (081 B-1).
                remedy = remedy_text("money_armed", tool=tool)
            # Reserved (money) tools share one text: one line, not one per tool.
            key = (gate, tier, remedy) if tier == "reserved" else (tool, gate, tier, remedy)
            rows.setdefault(key, []).append(tool)
        for (*_t, gate, tier, remedy), tools in rows.items():
            lines.append(f"- {', '.join(tools)} [{tier}] gate: {gate} → {remedy}")
        lines.append("Reserved tools are owner-only by design; their absence never blocks "
                     "other work."
                     + (" Goals you create carry their OWN tools: "
                        + ", ".join(sorted(grantable)) + "." if can_create_goals else ""))
        lines.append("</tool-availability>")
        return "\n".join(lines)
    except Exception:
        logger.debug("tool availability note failed (fail-open)", exc_info=True)
        return ""
