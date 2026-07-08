"""Delegation policy for the `delegate_task` tool (roadmap P1, Reference §29).

Pure, side-effect-free decision logic for "may this agent delegate, and what
role does the spawned child get?" Keeping it free of Controller/orchestrator
state makes the role + depth gate unit-testable in isolation (same approach as
the pre_tool_call hook seam).

Roles
-----
- ``leaf`` (default): the spawned sub-agent cannot delegate further.
- ``orchestrator``: the agent may call ``delegate_task`` (subject to depth).

The main (top-level) agent defaults to ``orchestrator`` so today's single-level
delegation keeps working; sub-agents are spawned as ``leaf``. The depth limit
(``MAX_SUB_AGENT_DEPTH``, default 1) is the independent backstop against runaway
recursion — role and depth are checked together so the gate stays correct even
if the depth limit is later raised.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

Role = str  # "leaf" | "orchestrator"

ORCHESTRATOR: Role = "orchestrator"
LEAF: Role = "leaf"

# --- UP-05: sub-agent least-privilege toolset --------------------------------
#
# A delegated child inherits the parent's full toolset today (shared Controller),
# so a confused/injected child can run code, schedule cron jobs, or delegate
# recursively with the parent's authority. UP-05 narrows the child's toolset.
#
# Two distinct levers, because POLYROB registers tools two different ways:
#   1. DELEGATE_BLOCKED_TOOLS — *container tool_ids* stripped from the child's
#      load list (these are loaded via load_tools_from_container).
#   2. DELEGATION_ACTION_NAMES — the delegation *actions* (subtask/
#      parallel_subtasks/delegate_task) are NOT tool_ids; they are registered
#      unconditionally in Controller._register_default_actions when sub-agents
#      are enabled. They are suppressed on a leaf child via the Registry's
#      exclude_actions seam (see delegation_exclusions_for_child).
# NOTE: "task" is the TODO tool (task_todo_*), NOT delegation — do not block it.
DELEGATE_BLOCKED_TOOLS: frozenset = frozenset({
    "code_execution",  # local subprocess exec — not a sandbox (Item 3)
    "coding",          # str_replace/apply_patch/run_tests — leaf can't mutate/run the repo (H10-B, #4146 lesson)
    "cronjob",         # durable scheduled runs (CronJobTool)
    "x402_pay",        # agent crypto payments — never delegate to a leaf
    "x402_invoice",    # agent invoicing (outward-facing money) — never delegate to a leaf
    "hyperliquid",     # crypto trading — never delegate to a leaf
    "polymarket",      # crypto trading — never delegate to a leaf
    # P0: coding / self-evolution container tool_ids a leaf must never wield.
    "git",            # git write surface (push/commit) — leaf can't ship code
    "github",         # PR/merge/issue write — leaf can't ship code
    "process",        # background processes (P1) — leaf can't spawn long-running jobs
    "tool_manage",    # dynamic-tool authoring (P1) — leaf can't create/promote tools
    "mcp",            # MCP install path (P0/P2) — leaf can't install servers
    "shell",          # persistent sandbox shell (WS-2) — leaf never gets a shell
    "self_env",       # self-maintenance verbs (WS-5) — leaf never self-manages
})

# Delegation action names (registered by Controller._register_subtask_action).
DELEGATION_ACTION_NAMES: frozenset = frozenset({
    "subtask", "parallel_subtasks", "delegate_task",
})

_BLOCKED_TOOLS_ENV = "DELEGATE_BLOCKED_TOOLS"


def get_blocked_child_tools() -> frozenset:
    """Resolve the child tool_id blocklist (default + env override).

    ``DELEGATE_BLOCKED_TOOLS`` (comma list) **replaces** the default when set to a
    non-empty value; unset OR empty-string keeps the default. Whitespace/empty
    entries are ignored.
    """
    raw = os.getenv(_BLOCKED_TOOLS_ENV)
    if raw is None:
        return DELEGATE_BLOCKED_TOOLS
    entries = frozenset(t.strip() for t in raw.split(",") if t.strip())
    return entries if entries else DELEGATE_BLOCKED_TOOLS


def narrow_child_tools(
    *,
    parent_tools: List[str],
    requested_tools: Optional[List[str]],
    child_role: Role,
    blocked: Optional[frozenset] = None,
) -> List[str]:
    """Least-privilege child tool_ids = (parent ∩ requested) − blocked.

    ``requested_tools=None`` => inherit the parent's set. A child can never gain a
    tool the parent did not have (intersection). The blocklist always wins over an
    explicit request. ``child_role`` is accepted for symmetry with
    ``delegation_exclusions_for_child`` (delegation is an *action*, not a tool_id,
    so role does not re-add a tool_id here).
    """
    blocked = get_blocked_child_tools() if blocked is None else blocked
    # Distinguish "inherit all" (None) from "explicitly request none" ([]). Using
    # `not requested_tools` conflated the two, so requesting zero tools wrongly
    # inherited the full parent set — a least-privilege escalation.
    if requested_tools is None:
        base = list(parent_tools)
    else:
        base = [t for t in requested_tools if t in parent_tools]
    return [t for t in base if t not in blocked]


def delegation_exclusions_for_child(child_role: Role) -> frozenset:
    """Action names to exclude from a child Controller's registry.

    A ``leaf`` child cannot delegate, so the delegation actions are excluded at
    registration (defence-in-depth on top of the call-time ``evaluate_delegation``
    gate, and it removes the schema leakage). An ``orchestrator`` child (only
    reachable if depth > 1 and construction grants the role) keeps them.
    """
    return frozenset() if child_role == ORCHESTRATOR else DELEGATION_ACTION_NAMES

_DISABLED_REASON = (
    "Sub-agent system is disabled. Complete this work directly without "
    "delegation (set SUB_AGENTS_ENABLED=true to enable)."
)


@dataclass(frozen=True)
class DelegationDecision:
    """Outcome of a delegation request.

    allowed     — whether the caller may spawn the sub-agent(s).
    reason      — human/LLM-facing denial reason when ``not allowed`` (else None).
    child_role  — effective role for the spawned child, clamped so a child that
                  sits at the maximum depth can never itself be an orchestrator.
    """

    allowed: bool
    reason: Optional[str]
    child_role: Role


def evaluate_delegation(
    *,
    enabled: bool,
    caller_is_sub_agent: bool,
    caller_role: Role,
    requested_child_role: Role,
    max_depth: int,
    current_depth: Optional[int] = None,
) -> DelegationDecision:
    """Decide whether ``delegate_task`` may proceed and what role the child gets.

    Args:
        enabled: result of ``TimeoutConfig.get_sub_agents_enabled()``.
        caller_is_sub_agent: ``execution_context.is_sub_agent`` of the caller.
        caller_role: the caller agent's role ("leaf"/"orchestrator").
        requested_child_role: role requested for the spawned child.
        max_depth: ``MAX_SUB_AGENT_DEPTH`` (depth of the deepest allowed agent;
            1 means the main agent may spawn one level of leaves).
        current_depth: caller's depth (main = 0). Derived from
            ``caller_is_sub_agent`` when not supplied.
    """
    if not enabled:
        return DelegationDecision(False, _DISABLED_REASON, LEAF)

    if current_depth is None:
        current_depth = 1 if caller_is_sub_agent else 0

    # A leaf agent may never delegate.
    if caller_role != ORCHESTRATOR:
        return DelegationDecision(
            False,
            "This agent has role 'leaf' and cannot delegate. Complete the work "
            "directly.",
            LEAF,
        )

    # Depth backstop: the caller must have room for at least one more level.
    if current_depth >= max_depth:
        return DelegationDecision(
            False,
            f"Maximum delegation depth ({max_depth}) reached. Complete these "
            "tasks directly without spawning more sub-agents.",
            LEAF,
        )

    # Clamp the child's role: it can only be an orchestrator if it would still
    # have room to delegate (i.e. it does not sit at the maximum depth).
    child_depth = current_depth + 1
    if requested_child_role == ORCHESTRATOR and child_depth < max_depth:
        child_role = ORCHESTRATOR
    else:
        child_role = LEAF

    return DelegationDecision(True, None, child_role)
