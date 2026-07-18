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

logger = logging.getLogger(__name__)

# tool_id -> (gate description, tier, remedy shown to the agent)
GATED_TOOL_REGISTRY: dict = {
    "twitter":        ("TWITTER_ENABLED", "disabled",
                       "owner sets TWITTER_ENABLED=true (auto-ON in AUTONOMY_MODE=autonomous)"),
    "mcp":            ("MCP_ENABLED + config/mcp_config.json", "disabled",
                       "owner sets MCP_ENABLED=true and configures servers"),
    "email":          ("SMTP/IMAP credentials + outbound policy", "disabled",
                       "owner configures email creds; sends follow outbound.policy"),
    "x402_invoice":   ("X402_INVOICE_ENABLED", "disabled",
                       "owner sets X402_INVOICE_ENABLED=true (auto-ON in autonomous mode)"),
    "knowledge":      ("KB_ENABLED", "disabled", "owner sets KB_ENABLED=true"),
    "browser":        ("session tool_ids", "loadable", "request tool_ids=['browser']"),
    "coding":         ("CODING_TOOLS_ENABLED", "disabled", "owner sets CODING_TOOLS_ENABLED=true"),
    "goal":           ("GOALS_ENABLED", "disabled", "owner sets GOALS_ENABLED=true"),
    "cronjob":        ("CRON_ENABLED (AUTONOMY_POSTURE>=full)", "disabled",
                       "owner raises AUTONOMY_POSTURE or sets CRON_ENABLED=true"),
    "code_execution": ("CODE_EXEC_ENABLED / AGENT_COMPUTE_POSTURE>=1", "disabled",
                       "owner raises AGENT_COMPUTE_POSTURE (host axis; not part of AUTONOMY_MODE)"),
    "shell":          ("AGENT_COMPUTE_POSTURE>=1", "disabled",
                       "owner raises AGENT_COMPUTE_POSTURE"),
    "x402_pay":       ("money-SPEND", "reserved",
                       "owner-only, explicitly enabled, never autonomous; raise an ask if truly needed"),
    "hyperliquid":    ("trading", "reserved", "owner-only; trading is never autonomous"),
    "polymarket":     ("trading", "reserved", "owner-only; trading is never autonomous"),
}


def _hint_enabled() -> bool:
    from agents.task.constants import _bool_env
    return _bool_env("TOOL_AVAILABILITY_HINT", True)


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
            lines.append(f"- {tool} [{tier}] gate: {gate} → {remedy}")
        lines.append("Reserved tools are owner-only by design; their absence never blocks "
                     "other work. Goals you create carry their OWN tools: "
                     + ", ".join(grantable_autonomous_tools()) + ".")
        lines.append("</tool-availability>")
        return "\n".join(lines)
    except Exception:
        logger.debug("tool availability note failed (fail-open)", exc_info=True)
        return ""
