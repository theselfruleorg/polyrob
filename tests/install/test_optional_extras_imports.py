"""Clean-room import guards for optional extras (proposal 027, WP1/WP8).

A plain ``pip install polyrob`` ships NO optional extra (playwright, fastapi,
uvicorn, aiogram, ...). The core agent import chain must still import — the
0.10.0 wheel died with "Task package not available" because module-scope
playwright imports sat on the task-agent import path (tools/browser/browser.py,
tools/browser/context.py, tools/dom/service.py).

These tests import the load-bearing modules in a subprocess with the extra
BLOCKED at the import system level — the dev tree always has the extras
installed, so blocking is the only honest way to reproduce a core-only install.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_BLOCKER = """
import sys

_BLOCKED = {blocked!r}

class _ExtraBlocker:
    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".")[0]
        if root in _BLOCKED:
            raise ModuleNotFoundError(f"No module named {{fullname!r}} (blocked)")
        return None

sys.meta_path.insert(0, _ExtraBlocker())

import {module}
print("IMPORT_OK")
"""


def _import_with_blocked(module: str, blocked: tuple) -> subprocess.CompletedProcess:
    code = _BLOCKER.format(blocked=set(blocked), module=module)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.parametrize(
    "module",
    [
        # The exact chain TaskAgent._initialize walks (task_agent_lite.py:317).
        "agents.task.agent.session",
        # The registry used to import BrowserContext at module scope.
        "tools.controller.registry.service",
        # The two browser modules themselves must be import-safe (lazy playwright).
        "tools.browser.context",
        "tools.browser.browser",
        "tools.browser.browser_manager",
        "tools.dom.service",
    ],
)
def test_core_agent_imports_without_playwright(module):
    proc = _import_with_blocked(module, ("playwright",))
    assert proc.returncode == 0 and "IMPORT_OK" in proc.stdout, (
        f"{module} must import without playwright (core install has no [browser] "
        f"extra).\nstderr:\n{proc.stderr[-2000:]}"
    )


def test_every_cli_command_module_imports_with_all_extras_blocked():
    """Ratchet: the CLI layer's lazy-import discipline. Every command module
    must import on a core-only install (extras fail at command time with the
    pip remedy, never at import time)."""
    modules = sorted(
        f"cli.commands.{p.stem}"
        for p in (REPO_ROOT / "cli" / "commands").glob("*.py")
        if p.stem != "__init__"
    )
    blocked = ("playwright", "fastapi", "uvicorn", "aiogram", "web3",
               "sentence_transformers", "tweepy", "faster_whisper")
    code = _BLOCKER.format(blocked=set(blocked), module="importlib") + "".join(
        f"\nimportlib.import_module({m!r})" for m in modules
    ) + "\nprint('ALL_CLI_OK')"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0 and "ALL_CLI_OK" in proc.stdout, (
        "a CLI command module import-crashed on a core-only install:\n"
        + proc.stderr[-2000:]
    )


def test_browser_launch_without_playwright_names_the_remedy():
    """With playwright absent, launching the browser must fail with the pip
    remedy — not a bare ModuleNotFoundError at import time."""
    code = _BLOCKER.format(blocked={"playwright"}, module="tools.browser.browser") + """
import asyncio
from tools.browser.browser import Browser, BrowserConfig

async def main():
    b = Browser(config=BrowserConfig(headless=True))
    try:
        await b.get_playwright_browser()
    except Exception as e:
        print("LAUNCH_ERR:", e)
        return
    print("LAUNCH_OK")

asyncio.run(main())
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "LAUNCH_ERR:" in proc.stdout
    assert "polyrob[browser]" in proc.stdout, (
        "browser launch failure must name the [browser] extra remedy; got:\n"
        + proc.stdout
    )
