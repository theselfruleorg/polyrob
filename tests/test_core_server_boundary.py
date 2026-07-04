"""Guard test: the rob-core import graph must not reach the server tier.

C3 of the core/server split (docs/ROB_CORE_SERVER_SPLIT_SPEC.md §4): importing the
core agent runtime (TaskAgent, the agent loop, the CLI) must not import FastAPI,
billing/credits, auth, x402, or the webview. Each entry point is imported in a fresh
subprocess with the server-tier modules blocked via a meta-path finder — simulating a
rob-core-only environment where those packages are not installed.

If this test fails, a core module gained a top-level import of a server-tier module.
Make the import lazy (inside the function/conditional that needs it) or relocate the
shared symbol to a core-safe home (e.g. core/exceptions.py), with a back-compat
re-export from the old location.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Top-level packages that belong to the server tier (blocked outright).
BLOCKED_TOP_LEVEL = ("fastapi", "starlette", "uvicorn", "api", "webview", "socketio")
# Sub-packages of modules/ that belong to the server tier (modules/ itself is shared).
BLOCKED_PREFIXES = ("modules.credits", "modules.auth", "modules.x402")

BLOCKER_TEMPLATE = """
import sys

BLOCKED_TOP_LEVEL = {blocked_top_level!r}
BLOCKED_PREFIXES = {blocked_prefixes!r}


def _blocked(name):
    top = name.split(".")[0]
    return top in BLOCKED_TOP_LEVEL or any(
        name == p or name.startswith(p + ".") for p in BLOCKED_PREFIXES
    )


class FinderWrapper:
    \"\"\"Make server-tier packages unfindable, exactly as if they were not installed.

    Every existing meta-path finder is wrapped to return None for blocked names, so
    `import fastapi` raises a plain ModuleNotFoundError and feature-detection probes
    like `importlib.util.find_spec("fastapi")` return None — both identical to a
    rob-core-only environment.
    \"\"\"

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

import {entry_point}  # noqa: E402,F401

leaked = sorted(
    m for m in sys.modules
    if _blocked(m) and sys.modules[m] is not None
)
if leaked:
    raise SystemExit(f"server-tier modules leaked into sys.modules: {{leaked}}")

print("CORE_IMPORT_OK")
"""

CORE_ENTRY_POINTS = [
    "agents.task_agent_lite",  # TaskAgent — the spec's §4 verification target
    "agents.task.agent.service",  # the agent step loop + its core/ mixins
    "agents.task.agent.orchestrator",  # session lifecycle
    "cli.polyrob",  # the polyrob CLI entry point
]


@pytest.mark.parametrize("entry_point", CORE_ENTRY_POINTS)
def test_core_entry_point_imports_without_server_tier(entry_point):
    code = BLOCKER_TEMPLATE.format(
        blocked_top_level=BLOCKED_TOP_LEVEL,
        blocked_prefixes=BLOCKED_PREFIXES,
        entry_point=entry_point,
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0 and "CORE_IMPORT_OK" in result.stdout, (
        f"importing {entry_point} reached the server tier "
        f"(fastapi/credits/auth/x402/webview/api):\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
