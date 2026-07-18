"""Layering ratchet (WS-1, 2026-07-16).

Two invariants on the core tier's upward imports into ``agents.*``:

1. ``core/`` must NEVER import ``agents.task.constants`` — WS-1 relocated that cluster to
   ``core/config_policy/`` precisely to break the core<->agents.task cycle. Importing it back
   would re-open the cycle.
2. Every OTHER ``core/ -> agents.*`` edge must be in the frozen ALLOWLIST below, which may
   only SHRINK. Adding a NEW upward edge fails this test — relocate the shared symbol into the
   core tier instead (that is the WS-1/WS-5 programme). When you legitimately remove an edge
   (e.g. WS-5 moves ``surface_config`` / ``telemetry.event_log`` into core), delete its lines
   here so the ratchet tightens.

The scan is source-level (AST), so it catches both top-level and in-function (lazy) imports —
a lazy import still couples the tiers at the source level even if it defers the runtime pull.

See docs/plans/2026-07-16-ws1-config-relocation.md.
"""
import ast
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parent.parent / "core"

# (core-relative-path, imported-agents-module). May only SHRINK over WS-1 phases 3-4 + WS-5.
# `agents.task.constants` is deliberately ABSENT — WS-1 killed that edge.
ALLOWLISTED_CORE_TO_AGENTS_EDGES = frozenset({
    ('core/autonomy_runtime.py', 'agents.task.agent.autonomy_state'),
    ('core/autonomy_runtime.py', 'agents.task.agent.core.curator'),
    ('core/autonomy_runtime.py', 'agents.task.goals.board'),
    ('core/autonomy_runtime.py', 'agents.task.goals.dispatcher'),
    ('core/autonomy_runtime.py', 'agents.task.surface_config'),
    ('core/bootstrap.py', 'agents.task.path'),
    ('core/bootstrap.py', 'agents.task_agent_lite'),
    ('core/credit_sentinel.py', 'agents.task.telemetry.event_log'),
    ('core/initialization.py', 'agents'),
    ('core/initialization.py', 'agents.task.agent.session'),
    ('core/interactive_gate.py', 'agents.task.utils'),
    ('core/knowledge_export.py', 'agents.task.agent.skill_manager'),
    ('core/knowledge_export.py', 'agents.task.goals.board'),
    ('core/recap.py', 'agents.task.telemetry.event_log'),
    ('core/self_evolution.py', 'agents.task.agent.skill_manager'),
    ('core/self_evolution.py', 'agents.task.telemetry.event_log'),
    ('core/self_evolution.py', 'agents.task.telemetry.self_events'),
    ('core/surfaces/binding.py', 'agents.task.surface_config'),
    ('core/surfaces/bootstrap.py', 'agents.task.surface_config'),
    ('core/surfaces/correspondents.py', 'agents.task.surface_config'),
    ('core/surfaces/dispatcher.py', 'agents.task.surface_config'),
    ('core/surfaces/inbound_webhook.py', 'agents.task.surface_config'),
    ('core/surfaces/message_router.py', 'agents.task.surface_config'),
    ('core/surfaces/outbound_mirror.py', 'agents.task.surface_config'),
    ('core/surfaces/outbound_policy.py', 'agents.task.telemetry.event_log'),
    ('core/surfaces/owner_admin.py', 'agents.task.surface_config'),
    ('core/surfaces/proactive.py', 'agents.task.surface_config'),
    ('core/surfaces/seed.py', 'agents.task.surface_config'),
    ('core/surfaces/seed.py', 'agents.task.telemetry.event_log'),
    ('core/surfaces/transcription.py', 'agents.task.surface_config'),
    ('core/surfaces/user_delivery.py', 'agents.task.goals.autonomy_marker'),
    ('core/surfaces/user_delivery.py', 'agents.task.telemetry.event_log'),
    ('core/tickers.py', 'agents.task.telemetry.event_log'),
    ('core/wallet/factory.py', 'agents.task.telemetry.event_log'),
})


def _core_agents_edges():
    edges = set()
    for py in CORE_DIR.rglob("*.py"):
        rel = py.relative_to(CORE_DIR.parent).as_posix()
        tree = ast.parse(py.read_text(), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module == "agents" or node.module.startswith("agents."):
                    edges.add((rel, node.module))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "agents" or alias.name.startswith("agents."):
                        edges.add((rel, alias.name))
    return edges


def test_core_does_not_import_agents_task_constants():
    """WS-1: the relocated cluster must never be imported back into core (cycle guard)."""
    offenders = sorted(e for e in _core_agents_edges() if e[1] == "agents.task.constants")
    assert not offenders, (
        "core/ imports agents.task.constants — the WS-1 cycle regressed. Import the symbol "
        f"from core.config_policy instead:\n{offenders}"
    )


def test_core_to_agents_edges_only_shrink():
    """No NEW core->agents upward edge; relocate shared symbols into core instead."""
    new = sorted(e for e in _core_agents_edges() if e not in ALLOWLISTED_CORE_TO_AGENTS_EDGES)
    assert not new, (
        "New core->agents import(s) introduced. Relocate the shared symbol into the core tier "
        "instead of importing upward (see docs/plans/2026-07-16-ws1-config-relocation.md):\n"
        f"{new}"
    )


def test_import_core_config_policy_pulls_no_agents():
    """`import core.config_policy` must not drag any agents.* module into sys.modules."""
    import subprocess
    import sys

    code = (
        "import sys, core.config_policy; "
        "leaked = sorted(m for m in sys.modules if m == 'agents' or m.startswith('agents.')); "
        "print('LEAKED:' + ','.join(leaked)); "
        "raise SystemExit(1 if leaked else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=CORE_DIR.parent, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        "importing core.config_policy pulled agents.* modules:\n"
        f"{result.stdout}\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# 5-tier boundary ratchet (R-4, 2026-07-17)
#
# The documented layering (AGENTS.md): core <- modules <- agents <- tools <-
# {api, cli, surfaces, webview, cron}. A module may import DOWNWARD only.
# Every existing upward edge is seeded below; the list may only SHRINK — when
# you remove an edge (relocate the shared symbol into the lower tier), delete
# its row so the ratchet tightens. core->agents edges are governed by the
# stricter allowlist above and are EXCLUDED here so each edge has exactly one
# bookkeeping home. Intra-tier-4 imports (e.g. surfaces->cli) are not policed.
# ---------------------------------------------------------------------------

REPO_ROOT = CORE_DIR.parent

TIER_OF_PACKAGE = {
    "core": 0,
    "modules": 1,
    "agents": 2,
    "tools": 3,
    "api": 4, "cli": 4, "surfaces": 4, "webview": 4, "cron": 4,
}

ALLOWLISTED_UPWARD_EDGES = frozenset({
    ('agents/task/agent/core/construction.py', 'tools.browser.context'),
    ('agents/task/agent/core/construction.py', 'tools.browser.views'),
    ('agents/task/agent/core/construction.py', 'tools.dom.views'),
    ('agents/task/agent/core/curator.py', 'cron.scheduler'),
    ('agents/task/agent/core/history_io.py', 'tools.browser.views'),
    ('agents/task/agent/core/llm_runner.py', 'tools.browser.context'),
    ('agents/task/agent/core/llm_runner.py', 'tools.browser.views'),
    ('agents/task/agent/core/llm_runner.py', 'tools.dom.views'),
    ('agents/task/agent/core/memory_writer.py', 'tools.browser.context'),
    ('agents/task/agent/core/memory_writer.py', 'tools.browser.views'),
    ('agents/task/agent/core/memory_writer.py', 'tools.dom.views'),
    ('agents/task/agent/core/next_action_internal.py', 'tools.browser.context'),
    ('agents/task/agent/core/next_action_internal.py', 'tools.browser.views'),
    ('agents/task/agent/core/next_action_internal.py', 'tools.dom.views'),
    ('agents/task/agent/core/run_loop.py', 'tools.browser.context'),
    ('agents/task/agent/core/run_loop.py', 'tools.browser.views'),
    ('agents/task/agent/core/run_loop.py', 'tools.dom.views'),
    ('agents/task/agent/core/step.py', 'tools.browser.context'),
    ('agents/task/agent/core/step.py', 'tools.browser.views'),
    ('agents/task/agent/core/step.py', 'tools.dom.views'),
    ('agents/task/agent/core/step_execution.py', 'tools.controller.execution_context'),
    ('agents/task/agent/core/tool_availability.py', 'tools.goal_tools'),
    ('agents/task/agent/message_manager/service.py', 'tools.browser.views'),
    ('agents/task/agent/message_manager/tests.py', 'tools.browser.views'),
    ('agents/task/agent/message_manager/tests.py', 'tools.dom.views'),
    ('agents/task/agent/messages/context_references.py', 'tools.web_fetch.fetcher'),
    ('agents/task/agent/orchestrator.py', 'tools.browser.browser'),
    ('agents/task/agent/orchestrator.py', 'tools.browser.browser_manager'),
    ('agents/task/agent/orchestrator.py', 'tools.browser.context'),
    ('agents/task/agent/orchestrator.py', 'tools.controller.service'),
    ('agents/task/agent/orchestrator.py', 'tools.descriptors'),
    ('agents/task/agent/orchestrator.py', 'tools.filesystem'),
    ('agents/task/agent/prompts.py', 'tools.anysite'),
    ('agents/task/agent/prompts.py', 'tools.browser.views'),
    ('agents/task/agent/service.py', 'tools.browser.context'),
    ('agents/task/agent/service.py', 'tools.browser.views'),
    ('agents/task/agent/service.py', 'tools.dom.views'),
    ('agents/task/agent/sub_agent_manager.py', 'tools.controller.delegation'),
    ('agents/task/agent/sub_agent_manager.py', 'tools.controller.service'),
    ('agents/task/agent/tests.py', 'tools.browser.actions'),
    ('agents/task/agent/tests.py', 'tools.controller.registry.service'),
    ('agents/task/agent/tests.py', 'tools.controller.registry.views'),
    ('agents/task/agent/tests.py', 'tools.controller.views'),
    ('agents/task/agent/tests.py', 'tools.dom.views'),
    ('agents/task/agent/tool_call_tracker.py', 'tools.mcp.validation_tracker'),
    ('agents/task/agent/views.py', 'tools.browser.views'),
    ('agents/task/agent/views.py', 'tools.controller.registry.views'),
    ('agents/task/agent/views.py', 'tools.controller.types'),
    ('agents/task/agent/views.py', 'tools.dom.history_tree_processor.service'),
    ('agents/task/agent/views.py', 'tools.dom.views'),
    ('agents/task/goals/dispatcher.py', 'cron.scheduler'),
    ('agents/task/goals/dispatcher.py', 'tools.goal_tools'),
    ('agents/task/goals/dispatcher.py', 'tools.hf_deploy'),
    ('agents/task/session/browser_pool.py', 'tools.browser.browser'),
    ('agents/task/session/cleanup.py', 'tools.shell.backend_pool'),
    ('agents/task/tool_defaults.py', 'tools.anysite'),
    ('agents/task/tool_defaults.py', 'tools.coding'),
    ('core/__init__.py', 'modules'),
    ('core/activity_evidence.py', 'modules.credits.unified_ledger'),
    ('core/activity_evidence.py', 'modules.memory.registry'),
    ('core/autonomy_runtime.py', 'cron.runner'),
    ('core/autonomy_runtime.py', 'modules.database.user_profiles'),
    ('core/autonomy_runtime.py', 'modules.x402.settlement_watcher'),
    ('core/autonomy_runtime.py', 'tools.code_exec'),
    ('core/autonomy_runtime.py', 'tools.code_exec.backends.docker'),
    ('core/autonomy_runtime.py', 'tools.cronjob_tools'),
    ('core/autonomy_runtime.py', 'tools.hf_deploy'),
    ('core/autonomy_runtime.py', 'tools.hf_deploy.reconcile'),
    ('core/autonomy_runtime.py', 'tools.hf_deploy.registry'),
    ('core/bootstrap.py', 'modules.llm.llm_client'),
    ('core/bootstrap.py', 'modules.llm.llm_manager'),
    ('core/bootstrap.py', 'modules.llm.profiles'),
    ('core/bootstrap.py', 'tools.anysite'),
    ('core/bootstrap.py', 'tools.descriptors'),
    ('core/config.py', 'modules.llm.profiles'),
    ('core/config.py', 'tools.mcp.config'),
    ('core/container.py', 'modules.llm'),
    ('core/container.py', 'tools.filesystem'),
    ('core/initialization.py', 'modules'),
    ('core/initialization.py', 'modules.auth.api_key_manager'),
    ('core/initialization.py', 'modules.auth.identity_mapper'),
    ('core/initialization.py', 'modules.auth.siwe_auth'),
    ('core/initialization.py', 'modules.auth.tier_manager'),
    ('core/initialization.py', 'modules.credits.balance_manager'),
    ('core/initialization.py', 'modules.database'),
    ('core/initialization.py', 'modules.database.user_mcp_servers'),
    ('core/initialization.py', 'modules.llm'),
    ('core/initialization.py', 'modules.memory'),
    ('core/initialization.py', 'modules.memory.cache_manager'),
    ('core/initialization.py', 'modules.payments.deposit_monitor'),
    ('core/initialization.py', 'modules.payments.treasury_sweeper'),
    ('core/initialization.py', 'modules.payments.wallet_generator'),
    ('core/initialization.py', 'tools'),
    ('core/initialization.py', 'tools.base_tool'),
    ('core/initialization.py', 'tools.controller.service'),
    ('core/initialization.py', 'tools.descriptors'),
    ('core/initialization.py', 'tools.mcp.user_mcp_service'),
    ('core/instance.py', 'modules.memory.task.threat_scan'),
    ('core/knowledge_export.py', 'modules.skills.skill_usage'),
    ('core/permissions.py', 'modules.memory.memory_manager'),
    ('core/prefs.py', 'modules.memory.task.threat_scan'),
    ('core/recap.py', 'modules.credits.unified_ledger'),
    ('core/recap.py', 'modules.skills.skill_usage'),
    ('core/runtime_config.py', 'modules.llm.profiles'),
    ('core/self_context_writer.py', 'modules.memory.task.threat_scan'),
    ('core/surfaces/continuity.py', 'modules.llm.messages'),
    ('core/surfaces/continuity.py', 'modules.memory.registry'),
    ('core/surfaces/message_router.py', 'modules.llm.brain_scrubber'),
    ('core/surfaces/transcription.py', 'modules.transcription'),
    ('modules/auth/identity_mapper.py', 'tools.alchemy.alchemy_tool'),
    ('modules/credits/unified_ledger.py', 'agents.task.telemetry.event_log'),
    ('modules/database/hyperliquid.py', 'tools.hyperliquid.models'),
    ('modules/database/hyperliquid.py', 'tools.mcp.security'),
    ('modules/database/polymarket.py', 'tools.mcp.security'),
    ('modules/database/polymarket.py', 'tools.polymarket.models'),
    ('modules/database/user_mcp_servers.py', 'tools.mcp.security'),
    ('modules/memory/episodic.py', 'agents.task.runtime'),
    ('modules/memory/task/reflection_service.py', 'agents.task.agent.core.aux_metering'),
    ('modules/memory/task/task_context_manager.py', 'agents.task.constants'),
    ('modules/x402/invoicing.py', 'agents.task.telemetry.event_log'),
    ('modules/x402/settlement_watcher.py', 'agents.task.goals.board'),
    ('modules/x402/settlement_watcher.py', 'agents.task.surface_config'),
    ('modules/x402/subscriptions.py', 'agents.task.telemetry.event_log'),
    ('tools/cronjob_tools.py', 'cron.jobs'),
    ('tools/cronjob_tools.py', 'cron.schedule'),
    ('tools/cronjob_tools.py', 'cron.service'),
})


def _upward_edges():
    edges = set()
    for pkg, my_tier in TIER_OF_PACKAGE.items():
        pkg_dir = REPO_ROOT / pkg
        if not pkg_dir.is_dir():
            continue
        for py in pkg_dir.rglob("*.py"):
            rel = py.relative_to(REPO_ROOT).as_posix()
            tree = ast.parse(py.read_text(), filename=str(py))
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    mods = [node.module]
                elif isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                for m in mods:
                    t = TIER_OF_PACKAGE.get(m.split(".")[0])
                    if t is not None and t > my_tier:
                        if pkg == "core" and m.split(".")[0] == "agents":
                            continue  # governed by ALLOWLISTED_CORE_TO_AGENTS_EDGES above
                        edges.add((rel, m))
    return edges


def test_upward_tier_edges_only_shrink():
    """No NEW upward cross-tier import; relocate the shared symbol downward instead."""
    new = sorted(e for e in _upward_edges() if e not in ALLOWLISTED_UPWARD_EDGES)
    assert not new, (
        "New upward cross-tier import(s). The layering is core <- modules <- agents <- "
        "tools <- {api,cli,surfaces,webview,cron}; move the shared symbol to the lower "
        f"tier (see docs/ops/HANDOFF-structural-remainder-2026-07-16.md R-4):\n{new}"
    )


def test_upward_allowlist_entries_still_exist():
    """Rows whose edge disappeared must be deleted (shrink-only hygiene)."""
    live = _upward_edges()
    stale = sorted(e for e in ALLOWLISTED_UPWARD_EDGES if e not in live)
    assert not stale, f"Delete these fixed rows from ALLOWLISTED_UPWARD_EDGES:\n{stale}"
