"""Layering ratchet (WS-1 2026-07-16; R-4 2026-07-17; W7 2026-10-10).

Invariants:

1. ``core/`` must NEVER import ``agents.task.constants`` — WS-1 relocated that cluster to
   ``core/config_policy/`` precisely to break the core<->agents.task cycle.
2. Every OTHER ``core/ -> agents.*`` edge must be in the frozen ALLOWLIST below, which may
   only SHRINK (a stale row fails too).
3. The tier ladder (``TIER_OF_PACKAGE``): a module imports DOWNWARD only; every live upward
   edge is an allowlisted row that may only shrink. ``"pkg.module:attr"`` string references
   (registry tables resolved by ``importlib`` at runtime) count as edges too — they have
   their own shrink-only list.
4. Cycles: the largest strongly-connected component of the MODULE graph (top-level AND
   function-level imports) that spans more than one package may only shrink (W7). The
   ladder alone cannot see a cycle closed by lazy imports.
5. The entry-point tier has no package-level cycle beyond the allowlisted rows.

The scan is source-level (AST), so it catches both top-level and in-function (lazy) imports —
a lazy import still couples the tiers at the source level even if it defers the runtime pull.
"""
import ast
import re
from collections import defaultdict
from pathlib import Path

from tests.support.repo_index import repo_index

CORE_DIR = Path(__file__).resolve().parent.parent / "core"
REPO_ROOT = CORE_DIR.parent

# (core-relative-path, imported-agents-module). May only SHRINK.
# `agents.task.constants` is deliberately ABSENT — WS-1 killed that edge.
ALLOWLISTED_CORE_TO_AGENTS_EDGES = frozenset({
    ('core/self_evolution.py', 'agents.task.agent.skill_manager'),
    # 044 T5: the room gate resolves high-impact-call policy from the SAME
    # correspondent-gate classification the taint gate uses (one policy, not
    # two), inside a function only — see core/surfaces/room_policy.py.
    ('core/surfaces/room_policy.py', 'agents.task.agent.core.correspondent_gate'),
})


def _core_agents_edges():
    edges = set()
    for py in CORE_DIR.rglob("*.py"):
        rel = py.relative_to(CORE_DIR.parent).as_posix()
        for node in repo_index().nodes(py):
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
        "instead of importing upward (WS-1 config relocation, 2026-07-16):\n"
        f"{new}"
    )


def test_core_to_agents_allowlist_entries_still_exist():
    """Rows whose edge disappeared must be deleted (shrink-only hygiene)."""
    live = _core_agents_edges()
    stale = sorted(e for e in ALLOWLISTED_CORE_TO_AGENTS_EDGES if e not in live)
    assert not stale, f"Delete these fixed rows from ALLOWLISTED_CORE_TO_AGENTS_EDGES:\n{stale}"


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
# The tier ladder (R-4 2026-07-17; W7 2026-10-10)
#
#   core, utils (0) <- modules (1) <- agents (2) <- tools (3) <- cron (4)
#     <- polyrob.app = the composition root, migrations (5)
#     <- api, cli, surfaces, webview = the entry points (6)
#     <- polyrob.sdk = the pack facade (7) <- packs (8)
#
# A module may import DOWNWARD (or within its tier) only. Every existing upward
# edge is seeded below; the list may only SHRINK — when you remove an edge,
# delete its row. core->agents edges are governed by the stricter allowlist
# above and are EXCLUDED here so each edge has exactly one bookkeeping home.
# ---------------------------------------------------------------------------

TIER_OF_PACKAGE = {
    "core": 0,
    # S1 (2026-08-28): utils is ranked WITH core (tier 0) — core/__init__ imports
    # utils, so utils can sit no higher.
    "utils": 0,
    "modules": 1,
    "agents": 2,
    "tools": 3,
    # cron imports nothing above tools; the store half is imported from below
    # (allowlisted rows), the runner half by the composition root.
    "cron": 4,
    # W7: the composition root (polyrob/app/) — wires every tier below, is called
    # by the entry points above. migrations sits with it (boot-time schema).
    "polyrob.app": 5,
    "migrations": 5,
    "api": 6, "cli": 6, "surfaces": 6, "webview": 6,
    # W11: the pack SDK — the one host surface a pack imports.
    "polyrob": 7,
    # The first-party packs (packs/<dir>/polyrob_<name>/) sit on top.
    "packs": 8,
}

_ENTRY_TIER = TIER_OF_PACKAGE["api"]
# The first-party pack packages in THIS tree (an out-of-tree pack is not ours).
_PACK_NAMES = frozenset(d.name for d in (REPO_ROOT / "packs").glob("*/polyrob_*") if d.is_dir())


def _package_of(module: str):
    """The TIER_OF_PACKAGE key of a dotted module name (None = not ours)."""
    top = module.split(".")[0]
    if top == "polyrob":
        return "polyrob.app" if module == "polyrob.app" or module.startswith("polyrob.app.") \
            else "polyrob"
    if top in _PACK_NAMES:
        return "packs"
    return top if top in TIER_OF_PACKAGE else None


def _tier_of(module: str):
    pkg = _package_of(module)
    return None if pkg is None else TIER_OF_PACKAGE[pkg]


_SKIP_PARTS = {"__pycache__", "node_modules", "dev"}


def _source_files():
    """``[(rel_path, module_name)]`` for every production .py in a tiered tree."""
    out = []
    roots = [(REPO_ROOT / pkg, REPO_ROOT) for pkg in TIER_OF_PACKAGE
             if "." not in pkg and pkg != "packs"]
    packs = REPO_ROOT / "packs"
    if packs.is_dir():
        roots += [(d, d.parent) for d in sorted(packs.glob("*/polyrob_*")) if d.is_dir()]
    for root, base in roots:
        if not root.is_dir():
            continue
        for py in root.rglob("*.py"):
            parts = py.relative_to(base).with_suffix("").parts
            if _SKIP_PARTS & set(parts):
                continue
            name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
            out.append((py.relative_to(REPO_ROOT).as_posix(), name))
    return out


def _imported_modules(rel: str, module: str):
    """``[(target, level, is_name)]`` for every import in the file (relative ones
    resolved). ``from X import name`` also yields ``(X.name, level, True)``, so a
    submodule imported by name is seen."""
    is_pkg = rel.endswith("__init__.py")
    out = []
    for node in repo_index().nodes(REPO_ROOT / rel):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = module.split(".")
                base = base if is_pkg else base[:-1]
                base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                target = ".".join(base + ([node.module] if node.module else []))
            else:
                target = node.module or ""
            if target:
                out.append((target, node.level, False))
                out.extend((f"{target}.{a.name}", node.level, True) for a in node.names)
        elif isinstance(node, ast.Import):
            out.extend((a.name, 0, False) for a in node.names)
    return out


# A "pkg.module:attr" string reference — a registry row that importlib resolves
# at runtime (core/tool_capabilities.py registrars, core/money_verbs.py,
# core/surfaces/catalog.py). The dependency is real, only
# deferred, so it is scanned like an import.
_STRING_REF_RE = re.compile(
    r"""["']((?:core|utils|modules|agents|tools|cron|migrations|api|cli|surfaces|webview"""
    r"""|polyrob|polyrob_[a-z_]+)(?:\.[A-Za-z_]\w*)+):[A-Za-z_]\w*["']"""
)


def _string_refs(rel: str):
    return set(_STRING_REF_RE.findall(repo_index().text(REPO_ROOT / rel)))


ALLOWLISTED_UPWARD_EDGES = frozenset({
    ('agents/task/agent/core/curator.py', 'cron.scheduler'),
    ('agents/task/agent/core/tool_availability.py', 'tools.goal_tools'),
    ('agents/task/agent/messages/context_references.py', 'tools.web_fetch.fetcher'),
    # W7: the DOM node's CSS-selector helper borrows the browser context's builder (lazy).
    ('agents/task/loop_types/dom_views.py', 'tools.browser.context'),
    ('agents/task/agent/orchestrator.py', 'tools.browser.browser'),
    ('agents/task/agent/orchestrator.py', 'tools.browser.browser_manager'),
    ('agents/task/agent/orchestrator.py', 'tools.browser.context'),
    ('agents/task/agent/orchestrator.py', 'tools.controller.service'),
    ('agents/task/agent/orchestrator.py', 'tools.descriptors'),
    ('agents/task/agent/orchestrator.py', 'tools.filesystem'),
    ('agents/task/agent/sub_agent_manager.py', 'tools.controller.delegation'),
    ('agents/task/agent/sub_agent_manager.py', 'tools.controller.service'),
    ('agents/task/agent/tool_call_tracker.py', 'tools.mcp.validation_tracker'),
    ('agents/task/goals/dispatcher.py', 'cron.scheduler'),
    ('agents/task/goals/dispatcher.py', 'tools.goal_tools'),
    ('agents/task/session/browser_pool.py', 'tools.browser.browser'),
    ('agents/task/session/cleanup.py', 'tools.shell.backend_pool'),
    ('agents/task/tool_defaults.py', 'tools.coding'),
    ('core/activity_evidence.py', 'modules.credits.unified_ledger'),
    ('core/activity_evidence.py', 'modules.memory.registry'),
    ('core/config.py', 'modules.llm.profiles'),
    ('core/config.py', 'tools.mcp.config'),
    ('core/instance.py', 'modules.memory.task.threat_scan'),
    ('core/prefs.py', 'modules.memory.task.threat_scan'),
    ('core/recap.py', 'modules.credits.unified_ledger'),
    ('core/recap.py', 'modules.skills.skill_usage'),
    ('core/runtime_config.py', 'modules.llm.profiles'),
    ('core/self_context_writer.py', 'modules.memory.task.threat_scan'),
    ('core/surfaces/continuity.py', 'modules.llm.messages'),
    ('core/surfaces/continuity.py', 'modules.memory.registry'),
    ('core/surfaces/message_router.py', 'modules.llm.brain_scrubber'),
    ('core/surfaces/transcription.py', 'modules.transcription'),
    ('modules/memory/episodic.py', 'agents.task.runtime'),
    # S6 (2026-08-29): the subscription-renewal ask store seam (notify mixin).
    ('modules/x402/settlement_notify.py', 'agents.task.goals.board'),
    ('tools/cronjob_tools.py', 'cron.jobs'),
    ('tools/cronjob_tools.py', 'cron.schedule'),
    ('tools/cronjob_tools.py', 'cron.service'),
    # W7: boot-time migrations snapshot through the CLI updater (one helper pair).
    ('migrations/boot.py', 'cli.update.context'),
    ('migrations/boot.py', 'cli.update.snapshot'),
})

# W7: upward "pkg.module:attr" registry rows. The registry pattern is fine (the
# lower tier names a registrar; the composition root resolves it), but the
# dependency is real — so it is DECLARED here and may only shrink. Move a table
# to the tier that owns its targets to delete rows.
ALLOWLISTED_UPWARD_STRING_REFS = frozenset({
    ('core/delivery_channels.py', 'polyrob_x.cron_delivery'),
    ('core/money_verbs.py', 'cli.ui.commands.h_check'),
    ('core/money_verbs.py', 'cli.ui.commands.h_holdings'),
    ('core/money_verbs.py', 'cli.ui.commands.h_send'),
    ('core/money_verbs.py', 'cli.ui.commands.h_swap'),
    ('core/money_verbs.py', 'surfaces.telegram.check_ops'),
    ('core/money_verbs.py', 'surfaces.telegram.holdings_ops'),
    ('core/money_verbs.py', 'surfaces.telegram.send_ops'),
    ('core/money_verbs.py', 'surfaces.telegram.swap_ops'),
    ('core/surfaces/catalog.py', 'cli.commands.discord'),
    ('core/surfaces/catalog.py', 'cli.commands.email'),
    ('core/surfaces/catalog.py', 'cli.commands.signal'),
    ('core/surfaces/catalog.py', 'cli.commands.slack'),
    ('core/surfaces/catalog.py', 'cli.commands.telegram'),
    ('core/surfaces/catalog.py', 'cli.commands.whatsapp'),
    # The per-tool table names each optional tool's registrar; the composition
    # root (polyrob/app/bootstrap.py) resolves it. core imports no tool module.
    ('core/tool_capabilities.py', 'tools.app_service'),
    ('core/tool_capabilities.py', 'tools.code_exec'),
    ('core/tool_capabilities.py', 'tools.coding'),
    ('core/tool_capabilities.py', 'tools.cronjob_tools'),
    ('core/tool_capabilities.py', 'tools.defi'),
    ('core/tool_capabilities.py', 'tools.git'),
    ('core/tool_capabilities.py', 'tools.github'),
    ('core/tool_capabilities.py', 'tools.goal_tools'),
    ('core/tool_capabilities.py', 'tools.hf_deploy'),
    ('core/tool_capabilities.py', 'tools.knowledge_ingest'),
    ('core/tool_capabilities.py', 'tools.publish'),
    ('core/tool_capabilities.py', 'tools.self_env'),
    ('core/tool_capabilities.py', 'tools.shell'),
    ('core/tool_capabilities.py', 'tools.x402'),
})


def _upward_string_refs():
    edges = set()
    for rel, module in _source_files():
        mine = _tier_of(module)
        for m in _string_refs(rel):
            t = _tier_of(m)
            if t is not None and t > mine:
                edges.add((rel, m))
    return edges


def _upward_edges():
    """Upward IMPORT edges. ``from a.b import c`` is one edge to ``a.b`` (the
    imported names are not separate rows)."""
    edges = set()
    for rel, module in _source_files():
        mine = _tier_of(module)
        for node in repo_index().nodes(REPO_ROOT / rel):
            mods = []
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mods = [node.module]
            elif isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            for m in mods:
                t = _tier_of(m)
                if t is None or t <= mine:
                    continue
                if rel.startswith("core/") and _package_of(m) == "agents":
                    continue  # governed by ALLOWLISTED_CORE_TO_AGENTS_EDGES above
                edges.add((rel, m))
    return edges


def test_upward_tier_edges_only_shrink():
    """No NEW upward cross-tier import; relocate the shared symbol downward instead."""
    new = sorted(e for e in _upward_edges() if e not in ALLOWLISTED_UPWARD_EDGES)
    assert not new, (
        "New upward cross-tier import(s). The ladder is TIER_OF_PACKAGE in this file "
        "(core <- modules <- agents <- tools <- cron <- polyrob.app <- entry points <- "
        f"packs); move the shared symbol to the lower tier:\n{new}"
    )


def test_upward_allowlist_entries_still_exist():
    """Rows whose edge disappeared must be deleted (shrink-only hygiene)."""
    live = _upward_edges()
    stale = sorted(e for e in ALLOWLISTED_UPWARD_EDGES if e not in live)
    assert not stale, f"Delete these fixed rows from ALLOWLISTED_UPWARD_EDGES:\n{stale}"


def test_upward_string_refs_only_shrink():
    """A ``"pkg.module:attr"`` registry row pointing UP the ladder is a dependency."""
    live = _upward_string_refs()
    new = sorted(live - ALLOWLISTED_UPWARD_STRING_REFS)
    stale = sorted(ALLOWLISTED_UPWARD_STRING_REFS - live)
    assert not new, (
        "New upward \"pkg.module:attr\" string reference(s). Put the table in the tier "
        f"that owns the target, or register through a lower-tier hook:\n{new}"
    )
    assert not stale, f"Delete these fixed rows from ALLOWLISTED_UPWARD_STRING_REFS:\n{stale}"


# ---------------------------------------------------------------------------
# Cycles (W7). The module graph counts top-level AND function-level imports
# (a lazy import defers the load, not the dependency). Before W7 one component
# held 588 modules across core/modules/agents/tools/cron/utils/cli/migrations:
# the composition root lived in core. The ceiling may only SHRINK.
#
# ⚠️ If you SPLIT a module that is already inside the component, the new modules
# join it: raise the ceiling by exactly that count in the same commit and say so.
# The tightness check has a small slack so a split elsewhere does not force
# every session to retune the number; lower it when you shrink the cycle.
# ---------------------------------------------------------------------------

# 588 before W7 -> 181 after the composition-root move and relocations -> 200 after
# the W9 god-file splits (core/status_*.py and tools/defi data_* modules joined it).
# +1 (W5): tools/controller/policy_chain.py, the hook chain split out of
# tools/controller/service.py (a module already inside the component).
# 081 WS-4.1: -> 143 when the skill catalog moved to core/skills and the writer
# to modules/skills (tools and core no longer import agents-tier skill modules).
LARGEST_CROSS_PACKAGE_SCC_CEILING = 143
_SCC_TIGHTNESS_SLACK = 10


_HEAD_FILES = None


def _head_files():
    global _HEAD_FILES
    if _HEAD_FILES is None:
        import subprocess
        out = subprocess.run(["git", "ls-tree", "-r", "--name-only", "-z", "HEAD"],
                             cwd=REPO_ROOT, capture_output=True, text=True, check=True,
                             timeout=60).stdout
        _HEAD_FILES = frozenset(p for p in out.split("\0") if p)
    return _HEAD_FILES


def _module_graph():
    # Committed files only (HEAD, not the index): the ceiling follows commits,
    # not another session's uncommitted or staged work in the shared tree.
    tracked = _head_files()
    files = [(rel, module) for rel, module in _source_files() if rel in tracked]
    known = {module for _rel, module in files}
    graph = defaultdict(set)
    for rel, module in files:
        for target, _level, _is_name in _imported_modules(rel, module):
            m = target
            while m and m not in known:
                m = m.rpartition(".")[0]
            if m and m != module:
                graph[module].add(m)
        graph.setdefault(module, set())
    return graph


def _sccs(graph):
    """Tarjan, iterative. Returns components with more than one module."""
    index, low, on, stack, out = {}, {}, set(), [], []
    counter = 0
    for root in graph:
        if root in index:
            continue
        work = [(root, iter(sorted(graph[root])))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(sorted(graph[w]))))
                    advanced = True
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1:
                    out.append(comp)
    return out


def _cross_package_sccs():
    return sorted((c for c in _sccs(_module_graph())
                   if len({_package_of(m) for m in c}) > 1), key=len, reverse=True)


def test_largest_cross_package_cycle_only_shrinks():
    comps = _cross_package_sccs()
    size = len(comps[0]) if comps else 0
    if comps:
        by_pkg = defaultdict(int)
        for m in comps[0]:
            by_pkg[_package_of(m)] += 1
        shape = dict(sorted(by_pkg.items()))
    else:
        shape = {}
    assert size <= LARGEST_CROSS_PACKAGE_SCC_CEILING, (
        f"The largest cross-package import cycle grew to {size} modules {shape} "
        f"(ceiling {LARGEST_CROSS_PACKAGE_SCC_CEILING}). A new import (lazy ones count) "
        "closed a cycle through a lower tier: register a hook in the lower tier, or move "
        "the shared symbol down. See the ⚠️ note above for a module split."
    )
    assert size > LARGEST_CROSS_PACKAGE_SCC_CEILING - _SCC_TIGHTNESS_SLACK, (
        f"The largest cross-package cycle shrank to {size} modules {shape}: lower "
        f"LARGEST_CROSS_PACKAGE_SCC_CEILING to {size} so the ratchet keeps the gain."
    )


# The entry points (api, cli, surfaces, webview) share a tier, so the ladder does
# not police them — but a package-level cycle between two of them is a tangle.
# These rows CLOSE the one live cycle (cli/commands/dashboard.py launches the
# console; the console reads three CLI helpers). Shrink-only.
ALLOWLISTED_ENTRY_TIER_CYCLE_EDGES = frozenset({
    ('webview/pages.py', 'cli.commands.doctor'),
    ('webview/pages.py', 'cli.commands.owner'),
    ('webview/pages.py', 'cli.config_store'),
})


def _entry_tier_package_edges():
    edges = defaultdict(set)
    for rel, module in _source_files():
        mine = _package_of(module)
        if TIER_OF_PACKAGE[mine] != _ENTRY_TIER:
            continue
        for target, level, is_name in _imported_modules(rel, module):
            if level or is_name:
                continue
            other = _package_of(target)
            if other and other != mine and TIER_OF_PACKAGE[other] == _ENTRY_TIER:
                edges[(mine, other)].add((rel, target))
    return edges


def test_entry_tier_has_no_new_package_cycle():
    edges = _entry_tier_package_edges()
    closing = set()
    for (a, b), rows in edges.items():
        if (b, a) in edges and a > b:
            closing |= rows  # the lexically larger package's side closes it
    new = sorted(closing - ALLOWLISTED_ENTRY_TIER_CYCLE_EDGES)
    stale = sorted(ALLOWLISTED_ENTRY_TIER_CYCLE_EDGES - closing)
    assert not new, (
        "A new package-level cycle between two entry points (api/cli/surfaces/webview). "
        f"Move the shared helper into a lower tier:\n{new}"
    )
    assert not stale, f"Delete these fixed rows from ALLOWLISTED_ENTRY_TIER_CYCLE_EDGES:\n{stale}"
