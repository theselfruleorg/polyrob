"""Ratchet guard: the polyrob-core import graph must not reach the platform tier.

Successor to tests/test_core_server_boundary.py for the core/platform split
(docs/superpowers/specs/2026-06-25-core-platform-extraction-design.md). Unlike the
older test, core now OWNS several api.* modules (task_http_api, a2a, openai_compat,
session_routing, models, interfaces, chat_via_task), so `api` is NOT blocked wholesale
— only platform api.* submodules by name, plus billing modules.* prefixes.

A core entry point can reach the platform tier in TWO ways, both counted as violations:
  1. LEAKED — the platform module loads into sys.modules (a lazy/conditional import that
     still resolves through a non-blocked path, leaving a blocked module resident).
  2. IMPORT_ERROR — a *top-level* import of a blocked platform module RAISES under the
     blocker (ModuleNotFoundError), so the entry point can't load at all in a core-only
     environment. This is the stronger coupling and MUST be tracked too: a lazy-import
     fix flips it from "raises" to "imports clean", which then makes its allowlist entry
     stale. (If the test only watched LEAKED, these entry points would pass vacuously.)

This is a RATCHET: VIOLATION_ALLOWLIST captures today's known violations (both kinds) so
the test PASSES now. Each seam-inversion task removes its entry. When the allowlist is
empty, a follow-up plan flips this to hard-fail. Do NOT add new entries to widen it.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Platform top-level packages (blocked outright). NOTE: `api` is intentionally absent.
BLOCKED_TOP_LEVEL = ("webview", "socketio")

# Platform sub-packages of modules/ (modules/ itself is shared).
BLOCKED_MODULE_PREFIXES = ("modules.credits", "modules.payments", "modules.x402", "modules.auth")

# Platform api.* submodules, blocked by exact name/prefix (core keeps the rest of api.*).
BLOCKED_API_MODULES = (
    "api.app",
    "api.middleware",
    "api.jwt_middleware",
    "api.conversation_manager",
    "api.dependencies",  # SPLIT file — allowlisted below until the auth seam lands
    "api.auth_endpoints",
    "api.auth_constants",
    "api.payment_endpoints",
    "api.payment_verification",
    "api.pricing_endpoints",
    "api.admin_endpoints",
    "api.x402_endpoints",
    "api.eip8004_endpoints",
    "api.polymarket_routes",
    "api.hyperliquid_routes",
    "api.mcp_routes",
    "api.skill_endpoints",
    "api.kb",
)

# Core entry points imported in a clean subprocess with the platform tier blocked.
CORE_ENTRY_POINTS = [
    "agents.task_agent_lite",
    "agents.task.agent.service",
    "agents.task.agent.orchestrator",
    "cli.polyrob",
    "core.initialization",
    "core.autonomy_runtime",
    "api.task_http_api",
    "api.a2a.endpoints",
    "api.a2a.streaming",
    "api.openai_compat.router",
    "api.session_routing",
    "api.chat_via_task",
    "tools.x402.service",
]

# Known violations that exist TODAY (LEAKED and/or IMPORT_ERROR). Each future seam task
# removes its entry; goal = {}. Values are normalized blocked-prefix keys (the value
# _normalize() produces for an offending module name).
#
# NOTE (2026-06-25 audit): No entry point currently LEAKS a platform module into
# sys.modules. Three entry points have a *top-level* import of a blocked platform module
# that RAISES under the blocker (IMPORT_ERROR) — tracked here as real violations:
#   api.a2a.endpoints        → api.a2a.task_handler imports `api.dependencies` at module
#                              level (task_handler.py:26); endpoints.py also imports
#                              api.payment_verification (:30) + api.dependencies (:48),
#                              but task_handler raises first → fails on api.dependencies.
#   api.a2a.streaming        → imports api.a2a.task_handler → api.dependencies (:26).
#   api.openai_compat.router → top-level `from api.payment_verification import ...` (:11).
# A lazy-import fix flips each from "raises" to "imports clean", making its entry stale.
VIOLATION_ALLOWLIST: dict[str, list[str]] = {
    "api.a2a.endpoints": ["api.dependencies"],
    "api.a2a.streaming": ["api.dependencies"],
    "api.openai_compat.router": ["api.payment_verification"],
}

BLOCKER_TEMPLATE = """
import sys

BLOCKED_TOP_LEVEL = {blocked_top_level!r}
BLOCKED_MODULE_PREFIXES = {blocked_module_prefixes!r}
BLOCKED_API_MODULES = {blocked_api_modules!r}


def _blocked(name):
    top = name.split(".")[0]
    if top in BLOCKED_TOP_LEVEL:
        return True
    for p in BLOCKED_MODULE_PREFIXES:
        if name == p or name.startswith(p + "."):
            return True
    for p in BLOCKED_API_MODULES:
        if name == p or name.startswith(p + "."):
            return True
    return False


class FinderWrapper:
    def __init__(self, finder):
        self._finder = finder

    def find_spec(self, name, path=None, target=None):
        if _blocked(name):
            return None
        find_spec = getattr(self._finder, "find_spec", None)
        if find_spec is None:
            return None
        return find_spec(name, path, target)

    def __getattr__(self, attr):
        return getattr(self._finder, attr)


sys.meta_path = [FinderWrapper(f) for f in sys.meta_path]

try:
    import {entry_point}  # noqa: E402,F401
    import_error = None
except Exception as exc:  # an allowlisted top-level import will raise here
    import_error = repr(exc)

leaked = sorted(m for m in sys.modules if _blocked(m) and sys.modules[m] is not None)
print("LEAKED:" + ",".join(leaked))
print("IMPORT_ERROR:" + (import_error or ""))
"""


_MISSING_MODULE_RE = re.compile(r"No module named ['\"]([\w.]+)['\"]")


def _is_blocked(name):
    """Mirror the subprocess `_blocked` so the parent can classify a module name."""
    top = name.split(".")[0]
    if top in BLOCKED_TOP_LEVEL:
        return True
    for p in BLOCKED_MODULE_PREFIXES + BLOCKED_API_MODULES:
        if name == p or name.startswith(p + "."):
            return True
    return False


def _normalize_one(name):
    """Collapse a single module name to its allowlist key (blocked-prefix or top pkg)."""
    for p in BLOCKED_MODULE_PREFIXES + BLOCKED_API_MODULES:
        if name == p or name.startswith(p + "."):
            return p
    return name.split(".")[0]


def _normalize(leaked_names):
    """Collapse leaked module names to their allowlist keys (top platform package)."""
    return {_normalize_one(name) for name in leaked_names}


def _module_from_import_error(import_error):
    """Return (module_name, is_platform) parsed from an IMPORT_ERROR repr, or None.

    Only a ModuleNotFoundError naming a *blocked platform* module counts as a violation.
    A genuine unrelated import failure (some other module missing) is surfaced as
    is_platform=False so the caller can fail loudly rather than swallow a real bug.
    """
    if not import_error:
        return None
    m = _MISSING_MODULE_RE.search(import_error)
    if not m:
        # An import error we can't parse a module name from — treat as non-platform so
        # the caller surfaces it instead of silently passing.
        return ("<unparsed>", False)
    name = m.group(1)
    return (name, _is_blocked(name))


def _run_entry_point(entry_point):
    """Import an entry point in the blocked subprocess; return (leaked, import_error, result)."""
    code = BLOCKER_TEMPLATE.format(
        blocked_top_level=BLOCKED_TOP_LEVEL,
        blocked_module_prefixes=BLOCKED_MODULE_PREFIXES,
        blocked_api_modules=BLOCKED_API_MODULES,
        entry_point=entry_point,
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    leaked_line = next(
        (ln for ln in result.stdout.splitlines() if ln.startswith("LEAKED:")), "LEAKED:"
    )
    leaked = [x for x in leaked_line[len("LEAKED:"):].split(",") if x]
    err_line = next(
        (ln for ln in result.stdout.splitlines() if ln.startswith("IMPORT_ERROR:")),
        "IMPORT_ERROR:",
    )
    import_error = err_line[len("IMPORT_ERROR:"):]
    return leaked, import_error, result


def _violations_for(entry_point):
    """Compute the normalized violation set for an entry point.

    Violation = LEAKED platform modules UNION the blocked module that caused IMPORT_ERROR.
    Raises AssertionError if the entry point failed to import for a NON-platform reason
    (a genuine bug must not masquerade as "no violation").
    """
    leaked, import_error, result = _run_entry_point(entry_point)
    actual = _normalize(leaked)
    parsed = _module_from_import_error(import_error)
    if parsed is not None:
        missing, is_platform = parsed
        if is_platform:
            actual.add(_normalize_one(missing))
        else:
            raise AssertionError(
                f"{entry_point} failed to import for a NON-platform reason "
                f"(missing module: {missing!r}). This is a genuine bug, not a boundary "
                f"violation — fix the import.\n--- stdout ---\n{result.stdout}\n"
                f"--- stderr ---\n{result.stderr}"
            )
    return actual, result


@pytest.mark.parametrize("entry_point", CORE_ENTRY_POINTS)
def test_core_entry_point_within_allowlist(entry_point):
    actual, result = _violations_for(entry_point)
    allowed = set(VIOLATION_ALLOWLIST.get(entry_point, []))
    unexpected = actual - allowed
    assert not unexpected, (
        f"{entry_point} reached NEW platform imports {sorted(unexpected)} "
        f"(allowed: {sorted(allowed)}).\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )


def test_allowlist_has_no_stale_entries():
    """Every allowlisted violation must still actually fire (LEAKED or IMPORT_ERROR)."""
    assert VIOLATION_ALLOWLIST, "allowlist is empty — nothing to verify (delete this guard)"
    stale = []
    for entry_point, allowed in VIOLATION_ALLOWLIST.items():
        actual, _ = _violations_for(entry_point)
        for entry in allowed:
            if entry not in actual:
                stale.append((entry_point, entry))
    assert not stale, f"Stale allowlist entries (no longer violate — remove them): {stale}"


def test_burn_down_count_reported(capsys):
    """Emit the remaining-violation count for CI visibility.

    Counts both LEAKED and IMPORT_ERROR violations (the allowlist holds both kinds), so
    the starting count is non-zero while top-level platform imports remain.
    """
    remaining = sum(len(v) for v in VIOLATION_ALLOWLIST.values())
    print(f"CORE_PLATFORM_BOUNDARY burn-down: {remaining} violations remaining")
    assert remaining >= 0
