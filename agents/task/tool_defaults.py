"""Single source of truth for per-surface default tool lists.

Three call sites historically hard-coded their own defaults and drifted. This
module is the one place that answers "what tools does a session get when the
caller didn't ask for a specific set?" — keyed by surface (server vs CLI/local).

Named toolsets (TOOLSETS) provide a stable vocabulary for choosing a pre-built
tool configuration. All ids are cross-checked against VALID_TOOL_IDS from
skill_manager — only valid ids appear in any set.  ``code_execution`` is
intentionally absent from every set (unsafe by default).

Notes on ids that were considered but excluded from the named TOOLSETS below:
- ``twitter``, ``goal``, ``cronjob``, ``knowledge``: now valid ids (VALID_TOOL_IDS,
  skill_manager.py) — excluded from these named GROUPS by policy, not vocabulary;
  ``goal``/``twitter``/``knowledge`` are granted via ``AUTONOMY_MODE=autonomous``
  (``agents.task.constants.AUTONOMOUS_MODE_TOOLS``, consumed by
  ``server_default_tools``/``default_goal_tools`` below), not by a named toolset.
- ``code_execution``: deliberately excluded (unsafe without a sandbox).
"""

import os

# ---------------------------------------------------------------------------
# Named toolsets registry
# ---------------------------------------------------------------------------
# Every id listed here must be a member of VALID_TOOL_IDS (see
# agents/task/agent/skill_manager.py — parity-tested against the tool registry by
# tests/unit/agents/task/test_valid_tool_ids_parity.py; don't re-enumerate the set
# here, a frozen copy went stale twice).
# code_execution is excluded from all named sets (unsafe by default).

TOOLSETS: dict[str, list[str]] = {
    # Absolute minimum: file access + task management.
    "minimal": ["filesystem", "task"],
    # Equivalent to the safe/cli minimum.
    "safe": ["filesystem", "task"],
    # Static base of the true default; resolve_toolset("default") adds the dynamic
    # coding/anysite additions so it is behavior-identical to an unset
    # POLYROB_AGENT_TOOLSET (`polyrob init` writes "default" — O1, 2026-07-14 review).
    "default": ["filesystem", "task", "web_fetch"],
    # Research workflow: lightweight web read + search + any-site scraping + read-only
    # crypto market data (no wallet).
    "research": ["filesystem", "task", "perplexity", "anysite", "web_fetch",
                 "polymarket_data", "hyperliquid_data"],
    # Trading research: research base focused on the crypto read tools (no trade tools).
    "trading_research": ["filesystem", "task", "perplexity", "anysite", "web_fetch",
                         "polymarket_data", "hyperliquid_data"],
    # Coding workflow: file editing + code runner tool.
    "coding": ["filesystem", "task", "coding"],
    # Development: coding + browser (e.g. to browse docs / test web UIs).
    "development": ["filesystem", "task", "coding", "browser"],
    # Browser-centric: just the browser on top of core.
    "browser": ["filesystem", "task", "browser"],
    # Social work: broad public discovery plus the two account-authoritative X rails.
    # Registration/config gates still apply; writes keep their per-action approval and
    # TWITTER_ENABLED boundaries, so requesting these ids does not bypass policy.
    "social": ["filesystem", "task", "anysite", "perplexity", "web_fetch",
               "twitter", "x_browser"],
    # Full server stack (mirrors server_default_tools()).
    "full": ["filesystem", "task", "web_fetch", "perplexity", "email", "mcp", "anysite"],
    # Flagship "earn real money, safely" goal toolset (scripts/seed_goal.py). Research /
    # browse / code only — deliberately NO money (wallet/x402), trading, or social tools;
    # those are opt-in per the safety envelope, never seeded by the flagship goal.
    "earn": ["filesystem", "task", "browser", "perplexity", "mcp", "anysite", "coding"],
    # Owner interactive chat supervised default (surfaces/telegram/interactive_tools.py) —
    # scheduling belongs in the owner chat, so `goal` + the write-gated `twitter` are in.
    "owner_interactive": ["goal", "twitter", "web_fetch", "filesystem", "task"],
}


def _dynamic_default_tools() -> list[str]:
    """The true default tool list: static base + dynamic coding/anysite additions.

    This is what an unset ``POLYROB_AGENT_TOOLSET`` yields; ``resolve_toolset``
    routes ``"default"`` (and unknown names) here so choosing "default" by name —
    e.g. accepting the `polyrob init` wizard default — is behavior-identical to
    never setting the env at all. Never raises: a failed dynamic import falls
    back to the static base.
    """
    tools = list(TOOLSETS["default"])
    try:
        from tools.coding import coding_tools_enabled
        if coding_tools_enabled():
            tools.append('coding')
    except Exception:
        pass
    try:
        from tools.anysite import anysite_cli_enabled
        if anysite_cli_enabled():
            tools.append('anysite')
    except Exception:
        pass
    try:
        # Registering a container tool does NOT make it callable — the id must
        # also be in the session's loaded tool_ids. Read the flag from the tier-0
        # SSOT rather than `tools.defi` so this stays a DOWNWARD import (the
        # agents->tools allowlist in tests/test_layering_ratchet.py may only
        # shrink), and so the two readers can never disagree.
        from core.config_policy import defi_data_enabled
        if defi_data_enabled():
            tools.append('defi_data')
    except Exception:
        pass
    return tools


def resolve_toolset(name: str) -> list[str]:
    """Return the tool list for *name*, or the ``default`` set for unknown names.

    ``"default"`` (and any unknown name) resolves dynamically — see
    :func:`_dynamic_default_tools`. Never raises; always returns a non-empty list.
    """
    key = (name or "").strip().lower()
    if key == "default" or key not in TOOLSETS:
        return _dynamic_default_tools()
    return list(TOOLSETS[key])


# ---------------------------------------------------------------------------
# Per-surface defaults
# ---------------------------------------------------------------------------


def server_default_tools() -> list[str]:
    """Comprehensive default for a server-container session (web read + MCP available).

    Web reading defaults to the lightweight ``web_fetch`` tool; the heavyweight Playwright
    ``browser`` tool is opt-in (request ``tool_ids=['browser']`` or a browser-oriented toolset).

    Under effective ``AUTONOMY_MODE=autonomous`` (single-owner instance) the default
    widens to the full ``AUTONOMOUS_MODE_TOOLS`` grant, minus the meta ``goal``/``cronjob``
    ids (those are agent-callable capabilities, not a session's ambient toolset). Supervised
    (default/unset) is byte-identical to the prior return.

    ⚠️ The BARE constant, not ``autonomous_mode_tools()``, and that is
    DELIBERATE. The accessor folds in the ``DEFI_AGENT_AUTONOMY`` grant
    (``defi_trade``, ``launchpad``, ``dapp_browser``) — money-SPEND tools that
    belong to a session the owner deliberately armed, never to the AMBIENT
    default of a session that asked for no tools at all. Pinned both ways by
    ``tests/unit/agents/task/test_ambient_toolset_has_no_spend.py``; an
    alignment audit read this as drift precisely because nothing here said so.
    """
    from agents.task.constants import full_autonomy_enabled, AUTONOMOUS_MODE_TOOLS
    if full_autonomy_enabled():
        return [t for t in AUTONOMOUS_MODE_TOOLS if t not in ("goal", "cronjob")]
    return ['filesystem', 'task', 'web_fetch', 'perplexity', 'email', 'mcp', 'anysite']


def with_compute_tools(tools: list[str]) -> list[str]:
    """Append the posture-gated compute tools (code_execution/shell/coding) when
    AGENT_COMPUTE_POSTURE>=1. The ONE place that answers "what does posture>=1
    add to a toolset" (agents/task/goals/dispatcher.py and the telegram
    interactive toolset both delegate here — 014 A2). Mutates and returns
    *tools* for chaining. Posture 0 (or any resolver error) is a no-op."""
    try:
        from agents.task.constants import compute_posture
        if compute_posture() >= 1:
            for t in ("code_execution", "shell", "coding"):
                if t not in tools:
                    tools.append(t)
    except Exception:
        pass
    return tools


def default_session_tools() -> list[str]:
    """Default toolset for a bare SessionRequest (no explicit tools).

    Supervised: byte-identical to the historical literal
    ['browser','filesystem','task'] that lived in agents/task_agent_lite.py
    (three drifting copies, pre-014). Effective AUTONOMY_MODE=autonomous: the
    ambient autonomous grant — AUTONOMOUS_MODE_TOOLS minus the meta
    goal/cronjob ids (same exclusion rationale as server_default_tools above).
    ⚠️ Money-SPEND tools are structurally absent from AUTONOMOUS_MODE_TOOLS
    (013 §2.3), so this can never widen into them — which is why it reads the
    BARE constant rather than ``autonomous_mode_tools()``, whose
    `DEFI_AGENT_AUTONOMY` grant adds exactly those. (Precisely: `x402_invoice`
    IS money-classified but RECEIVE-side, and `coding` IS a compute tool — the
    older wording overclaimed on both. What actually holds, and what the test
    pins, is that no money-SPEND tool is ever in an ambient session's toolset.)
    """
    from agents.task.constants import full_autonomy_enabled, AUTONOMOUS_MODE_TOOLS
    if full_autonomy_enabled():
        return [t for t in AUTONOMOUS_MODE_TOOLS if t not in ("goal", "cronjob")]
    return ["browser", "filesystem", "task"]


def cli_default_tools() -> list[str]:
    """Default for the lightweight CLI container.

    When ``POLYROB_AGENT_TOOLSET`` is set, the tool list is driven by the named
    toolset via ``resolve_toolset``; unset behaviour is BYTE-IDENTICAL to the
    previous implementation.

    Either way the final list is intersected through ``cli_unavailable_tools`` so
    the agent is never advertised tools the CLI container can't register.
    """
    from core.bootstrap import cli_unavailable_tools

    toolset_name = os.environ.get("POLYROB_AGENT_TOOLSET", "").strip()

    if toolset_name:
        # Named-toolset path: drive the list from the registry.
        tools = resolve_toolset(toolset_name)
    else:
        # Unset path: the same dynamic default the "default" toolset resolves to.
        tools = _dynamic_default_tools()

    unavailable = set(cli_unavailable_tools(tools))
    return [t for t in tools if t not in unavailable]
