"""P5a (proposal 018): numeric env parsing never crashes the process.

The 2026-07-17 config review found 102 raw ``int(os.getenv(...))`` /
``float(os.getenv(...))`` sites; the import-frozen ones (agents/task/
constants.py class bodies, core/tickers.py) crash the WHOLE process at startup
on a stray ``"none"``/``"off"`` value. core/env gains the missing float
parser; the frozen sites route through the guarded parsers; a shrink-only
ratchet keeps the raw-parse count from growing back.
"""
import importlib
import subprocess
import sys

from core.env import float_env, int_env


def test_float_env_guards_garbage(monkeypatch):
    monkeypatch.setenv("SOME_FLOAT", "2.5")
    assert float_env("SOME_FLOAT", 1.0) == 2.5
    for bad in ("none", "off", "", "abc"):
        monkeypatch.setenv("SOME_FLOAT", bad)
        assert float_env("SOME_FLOAT", 1.5) == 1.5
    monkeypatch.delenv("SOME_FLOAT", raising=False)
    assert float_env("SOME_FLOAT", 3.0) == 3.0


def test_int_env_still_guards(monkeypatch):
    monkeypatch.setenv("SOME_INT", "none")
    assert int_env("SOME_INT", 7) == 7


def test_constants_import_survives_garbage_numeric_env():
    # The killer case: a stray "none" in an import-frozen numeric flag used to
    # raise ValueError at module import and take the whole process down.
    code = (
        "import os\n"
        "for var in ('MAX_MCP_PER_STEP', 'COMPACTION_COOLDOWN_STEPS', "
        "'ALLOWED_REASONING_TURNS', 'LOOP_DETECTION_WINDOW', "
        "'AGENT_STEP_TIMEOUT_SECONDS', 'SELF_WAKE_IDLE_BACKOFF_SEC', "
        "'AUTONOMY_HEARTBEAT_INTERVAL_SEC'):\n"
        "    os.environ[var] = 'none'\n"
        "import agents.task.constants\n"
        "import core.tickers\n"
        "print('IMPORT_OK')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, timeout=120)
    assert "IMPORT_OK" in out.stdout, out.stderr[-2000:]
