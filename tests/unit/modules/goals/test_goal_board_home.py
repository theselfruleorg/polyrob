"""The goal board lives in the modules tier (proposal 125 S29).

The board closure imports nothing from ``agents``, so a module (the x402
settlement notice) reaches it without an upward import; the old
``agents.task.goals.<name>`` paths are gone.
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[4]
CLOSURE = ("board", "board_asks", "board_deps", "board_types", "machines",
           "payload_keys", "run_cancel", "rail_answers", "claim_liveness")


@pytest.mark.parametrize("name", CLOSURE)
def test_the_old_agents_path_is_gone(name):
    assert importlib.util.find_spec(f"modules.goals.{name}") is not None
    assert importlib.util.find_spec(f"agents.task.goals.{name}") is None


def test_the_board_imports_no_agents_module():
    code = ("import sys; " + "; ".join(f"import modules.goals.{n}" for n in CLOSURE) + "; "
            "leaked = sorted(m for m in sys.modules if m == 'agents' or m.startswith('agents.')); "
            "print(leaked); raise SystemExit(1 if leaked else 0)")
    r = subprocess.run([sys.executable, "-c", code], cwd=_REPO, capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_pack_facade_exports_the_open_ask_status():
    import polyrob.sdk as sdk
    from core.goal_vocab import ASK_OPEN
    assert sdk.ASK_OPEN == ASK_OPEN
