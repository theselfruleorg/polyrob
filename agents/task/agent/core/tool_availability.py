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

from core.tool_capabilities import gate_disclosure_map

logger = logging.getLogger(__name__)

# tool_id -> (gate description, tier, remedy shown to the agent). A VIEW (067 P1) of
# the ``gate`` field of the per-tool rows in core/tool_capabilities.py — declare a
# tool's disclosure there. Its membership differs from TOOL_GATE_FLAGS on purpose
# (see the note above gate_flag_map there).
GATED_TOOL_REGISTRY: dict = gate_disclosure_map()


def _hint_enabled() -> bool:
    from agents.task.constants import _bool_env
    return _bool_env("TOOL_AVAILABILITY_HINT", True)


#: The compute tools whose gate has TWO limbs (a flag AND the posture). Their
#: registry row is a static fallback; the live line comes from ``compute_gate``.
_COMPUTE_TOOLS = {
    "code_execution": ("CODE_EXEC_ENABLED", "code_exec_enabled"),
    "shell": ("SHELL_TOOLS_ENABLED", "shell_tools_enabled"),
}

_COMPUTE_SESSION_REMEDY = (
    "not in this session's toolset — goal/cron runs carry the compute tools only "
    "at posture>=1 with the flag on; otherwise run it in an interactive (owner) "
    "session or raise ONE ask")


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
                    "owner raises AGENT_COMPUTE_POSTURE (host axis; not part of AUTONOMY_MODE)")
        if not limbs[limb]:
            return (f"{flag_name}=false (posture {posture} already met)",
                    "disabled",
                    f"owner sets {flag_name}=true — raising the posture changes nothing here")
        return (f"{flag_name}=true, posture {posture}", "loadable", _COMPUTE_SESSION_REMEDY)
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


def build_tool_availability_note(loaded_tool_ids) -> str:
    """The <tool-availability> system-prompt block. '' when disabled or on any error."""
    if not _hint_enabled():
        return ""
    try:
        loaded = set(loaded_tool_ids or ())
        lines = ["<tool-availability>",
                 "Tools NOT in this session, with the reason and remedy — a missing tool is "
                 "NEVER a blocker you invent workarounds or excuses for: name the gap, use the "
                 "remedy, or raise ONE ask to the owner."]
        for tool, (gate, tier, remedy) in sorted(GATED_TOOL_REGISTRY.items()):
            if tool in loaded:
                continue
            if tool in _COMPUTE_TOOLS:
                gate, tier, remedy = compute_gate(tool)
            lines.append(f"- {tool} [{tier}] gate: {gate} → {remedy}")
        lines.append("Reserved tools are owner-only by design; their absence never blocks "
                     "other work. Goals you create carry their OWN tools: "
                     + ", ".join(grantable_autonomous_tools()) + ".")
        lines.append("</tool-availability>")
        return "\n".join(lines)
    except Exception:
        logger.debug("tool availability note failed (fail-open)", exc_info=True)
        return ""
