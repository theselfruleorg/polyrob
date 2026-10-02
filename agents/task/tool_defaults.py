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

# The ids live in core/config_policy/profiles.py (067 P1 — one ordered table);
# this dict is the historical ``name -> list`` view, key order = the table's
# (the `polyrob init` wizard offers them in that order).
from core.config_policy.profiles import (names as _profile_names, profile as _profile,
                                         provided_only as _provided_only)

TOOLSETS: dict[str, list[str]] = {
    name: list(_profile(f"toolset:{name}")) for name in _profile_names("toolset")
}


def _dynamic_default_tools() -> list[str]:
    """The true default tool list: static base + dynamic coding/anysite additions.

    This is what an unset ``POLYROB_AGENT_TOOLSET`` yields; ``resolve_toolset``
    routes ``"default"`` (and unknown names) here so choosing "default" by name —
    e.g. accepting the `polyrob init` wizard default — is behavior-identical to
    never setting the env at all. Never raises: a failed dynamic import falls
    back to the static base.
    """
    tools = list(_profile("default:cli"))
    try:
        from tools.coding import coding_tools_enabled
        if coding_tools_enabled():
            tools.append('coding')
    except Exception:
        pass
    # The anysite gate is registered by the discovery pack in the loader's phase 2
    # (core.tool_gates); unregistered (pack absent / not loaded) reads as off.
    from core.tool_gates import gate_on
    if gate_on("anysite"):
        tools.append('anysite')
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
    # TOOLSETS is an import-time view; a pack disabled at phase 2 drops out here.
    return _provided_only(TOOLSETS[key])


# ---------------------------------------------------------------------------
# Per-surface defaults
# ---------------------------------------------------------------------------


def _full_autonomy() -> bool:
    from agents.task.constants import full_autonomy_enabled
    return full_autonomy_enabled()


def _ambient_autonomous_tools() -> list[str]:
    """The BARE autonomous grant minus the meta ids (``exclude:ambient``) — the
    ambient toolset of an autonomous session that asked for no tools."""
    from agents.task.constants import AUTONOMOUS_MODE_TOOLS
    meta = _profile("exclude:ambient")
    return [t for t in _provided_only(AUTONOMOUS_MODE_TOOLS) if t not in meta]


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
    if _full_autonomy():
        return _ambient_autonomous_tools()
    return list(_profile("default:server"))


def with_compute_tools(tools: list[str]) -> list[str]:
    """Append the posture-gated compute tools (code_execution/shell/coding) when
    AGENT_COMPUTE_POSTURE>=1. The ONE place that answers "what does posture>=1
    add to a toolset" (agents/task/goals/dispatcher.py and the telegram
    interactive toolset both delegate here — 014 A2). Mutates and returns
    *tools* for chaining. Posture 0 (or any resolver error) is a no-op."""
    try:
        from agents.task.constants import compute_posture
        if compute_posture() >= 1:
            # 056 WS4: posture is the CEILING; each tool's own flag still decides.
            # Prod ran posture 1 with CODE_EXEC_ENABLED/SHELL_TOOLS_ENABLED off and
            # every goal record carried a false "[tool gap] code_execution …" line
            # (43 in 48 h) — the deploy was asking for tools it had switched off.
            from core.config_policy.capability_toggles import (
                code_exec_enabled, shell_tools_enabled, coding_tools_enabled)
            wanted = (("code_execution", code_exec_enabled),
                      ("shell", shell_tools_enabled),
                      ("coding", coding_tools_enabled))
            for t, enabled in wanted:
                try:
                    on = bool(enabled())
                except Exception:
                    on = False
                if on and t not in tools:
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
    if _full_autonomy():
        return _ambient_autonomous_tools()
    return list(_profile("default:session"))


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
