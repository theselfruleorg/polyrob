"""Guard test: entry points must not import heavy SDK/ML/Telegram modules at module load.

The agent runtime's heavy dependencies (LLM SDKs, torch/transformers, the Telegram
framework) belong behind a lazy boundary — loaded on first use inside the FastAPI
lifespan / the agent loop, never at the module import of an entry point. Importing
`cli.polyrob` (every CLI invocation) or `api.app` (every uvicorn worker boot) should
cost stdlib + framework only.

Each entry point is imported in a fresh subprocess and its resulting ``sys.modules`` is
checked against a forbidden set. If this fails, a module on that import path gained a
top-level import of a heavy dependency. Make the import lazy (inside the function /
lifespan that needs it) or relocate the shared symbol to an import-light home.

See docs/plans/2026-06-26-runtime-architecture-finalization-FUSION.md (the layer contract).
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Heavy, clearly-deferrable dependencies that must not load at entry-point import time.
HEAVY = (
    "torch",
    "transformers",
    "sentence_transformers",
    "aiogram",
    "openai",
    "anthropic",
    "google.generativeai",
)

_PROBE = """
import sys
import {target}
heavy = {heavy!r}
leaked = sorted(m for m in heavy if m in sys.modules)
print(",".join(leaked))
"""


def _heavy_modules_after_import(target: str) -> list[str]:
    """Import ``target`` in a clean subprocess; return which HEAVY modules leaked."""
    code = _PROBE.format(target=target, heavy=HEAVY)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"importing {target!r} failed:\n{result.stderr}"
    )
    out = result.stdout.strip()
    return out.split(",") if out else []


@pytest.mark.parametrize(
    "target",
    [
        "cli.polyrob",            # every CLI invocation
        "api.app",                # every uvicorn worker boot / redeploy
        "agents.task.constants",  # the leaf-config layer
    ],
)
def test_entry_point_does_not_import_heavy_deps(target):
    leaked = _heavy_modules_after_import(target)
    assert leaked == [], (
        f"importing {target!r} eagerly loaded heavy deps: {leaked}. "
        "Make these imports lazy (inside the function/lifespan that needs them)."
    )


def test_modules_llm_does_not_reach_up_into_agents():
    """L1 (modules.llm) must not import L2 (agents.*) at module load — no layering inversion."""
    code = (
        "import sys, modules.llm.profiles; "
        "print('agents' in sys.modules or 'aiogram' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False", (
        "importing modules.llm.profiles pulled in agents.* / aiogram — "
        "a capability lib reached up into the agent layer (fix llm_factory's "
        "top-level `from agents.task.constants import TimeoutConfig`)."
    )
