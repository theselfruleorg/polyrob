"""The ``POLYROB_LOCAL`` single-user profile: the interactive flag bucket, the autonomy flag
bucket, and ``local_mode_enabled``.

Split out of ``core/config_policy/policy.py`` (S2, 2026-08-29); ``policy.py`` re-exports
every name so existing importers are unaffected. Import order (a DAG): _env -> local_profile
-> autonomy_mode -> autonomy_posture -> compute_posture -> payment_policy -> capability_toggles
-> autonomy_config -> runtime_gates.
"""
from core.config_policy._env import _bool_env


# --- Local (terminal-native, single-user) profile -------------------------
# When POLYROB_LOCAL is truthy, the *interactive* tools a user drives (coding, git,
# knowledge base, RAG, project-context, messaging, prefs, invoice card, tool
# catalog) default ON as a group, so a terminal user gets them without setting ~10
# env vars. Multi-tenant server entry (main.py / uvicorn) never sets POLYROB_LOCAL,
# so its defaults are unchanged. An explicit per-flag value (e.g. KB_ENABLED=off)
# still wins — only the *default* moves.
#
# NOTE (0.9.0): this set is the INTERACTIVE bucket ONLY. The self-directed AUTONOMY
# loops (self-wake, goal board + planner, curator, background-review, episodic
# continuity, self-writing) were split out to _AUTONOMY_LOCAL_FLAGS below — they now
# default ON under POLYROB_LOCAL only when AUTONOMY_ENABLED is also on, so a
# first-time user isn't handed a background agent scheduling goals and rewriting its
# own skills without opting in.
#
# Excludes anything with a multi-tenant blast radius even on one machine:
# CODE_EXEC_ENABLED (not a sandbox) and the sub-agent concurrency caps.
_SAFE_LOCAL_FLAGS = frozenset({
    # Editor + structured git over the confined workspace (git_push is separately
    # approval-gated + leaf-blocked). Safe on a single-user CLI (own repo).
    "CODING_TOOLS_ENABLED",
    "GIT_TOOLS_ENABLED",
    # Knowledge base + auto-prefetch: read/write own KB on a single-user CLI.
    "KB_ENABLED",
    "KB_AUTO_PREFETCH",
    # Context-reference expansion (@file/@folder/@diff/@url) + auto-loaded
    # CLAUDE.md/AGENTS.md project context — trusted single-user workspace.
    "CONTEXT_REFERENCES_ENABLED",
    "PROJECT_CONTEXT_AUTOLOAD",
    # Gated `message` action (owner/allowlist -> MessageRouter). Forged/autonomous
    # turns are separately denied (MESSAGE_AUTONOMOUS_ALLOWLISTED).
    "MESSAGE_TOOL_ENABLED",
    # Read-only introspection + verify-before-done nudge + the typed `preferences`
    # action — all reads/own-state, safe on a single-user CLI.
    "AGENT_STATUS_TOOL",
    "VERIFY_BEFORE_DONE",
    "PREFS_TOOL_ENABLED",
    # The agent reading its OWN frozen face/voice, and copying that PNG into its
    # own session workspace so the existing `message(media_paths=…)` rail can
    # carry it. Read + one workspace-local write; it can never generate,
    # randomize, keep or push -- the identity ceremony is permanent and stays
    # the owner's.
    "AVATAR_TOOL_ENABLED",
    # Branded PNG invoice card alongside the text-only x402_request result
    # (presentation nicety, fail-open, never blocks the request).
    "INVOICE_CARD_ENABLED",
    # Dynamic tool rig: honest <tool-catalog> block + load_tool self-serve (money
    # tools stay explicit-grant-only; leaf/taint/posture/approval gates unchanged).
    "TOOL_PROGRESSIVE_DISCLOSURE",
})


# --- Autonomy bucket (self-directed loops) --------------------------------
# The subset of the local profile that is genuinely AUTONOMOUS — the agent acting on
# its own between the user's messages, or rewriting its own skills/identity. Under
# POLYROB_LOCAL these default ON only when AUTONOMY_ENABLED is also on (see
# _autonomy_group_default / autonomy_enabled). Server behavior is unchanged (local
# off => the whole group off). Keeping this as one named set makes "autonomy off" a
# single line a new user understands.
_AUTONOMY_LOCAL_FLAGS = frozenset({
    # W1 self-wake re-entry; W2 background review; W4 goal board + planner; W5
    # curator + note consolidation; W7 insights.
    "SELF_WAKE_ENABLED",
    "BACKGROUND_REVIEW_ENABLED",
    "GOALS_ENABLED",
    "GOAL_PLANNER_ENABLED",
    "CURATOR_ENABLED",
    "KNOWLEDGE_CURATOR_ENABLED",
    "INSIGHTS_TOOL",
    # Episodic activity ledger + proactive digest/continuity injection: passive
    # learning that surfaces prior activity proactively, so grouped with autonomy.
    "EPISODIC_MEMORY_ENABLED",
    "EPISODIC_DIGEST_INJECT",
    "CONTINUITY_BRIDGE_ENABLED",
    # §7.1 self-evolution transparency (unsolicited pending-proposal notice).
    "SELF_EVOLUTION_TRANSPARENCY",
    # Agent self-writing: skills / evolving SELF identity / owner-facts doc.
    "SKILLS_WRITABLE",
    "SELF_CONTEXT_WRITABLE",
    "OWNER_DOC_WRITABLE",
    # QW-1: goal/cron completion pushes attach file deliverables to the owner chat.
    "DELIVERABLES_ATTACH_ENABLED",
})


def local_mode_enabled() -> bool:
    """True when running as the single-user terminal-native agent.

    Canonical flag: ``POLYROB_LOCAL``. ``ROB_LOCAL`` is accepted as a deprecated
    back-compat alias (older docs/scripts referenced it) — either being truthy
    enables local mode, so a doc that still says ``ROB_LOCAL`` isn't a silent no-op.
    """
    return _bool_env("POLYROB_LOCAL", False) or _bool_env("ROB_LOCAL", False)


def _safe_autonomy_default(flag_name: str) -> bool:
    """Default for an INTERACTIVE local flag: ON under local mode, else OFF."""
    return local_mode_enabled() if flag_name in _SAFE_LOCAL_FLAGS else False
