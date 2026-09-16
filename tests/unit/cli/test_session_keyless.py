"""L11 (proposal 030 / 027 P1-4): read-only session verbs must work with ZERO keys.

`polyrob session list` used to route through ``cli_container()`` whose
non-interactive key preflight exits 1 on a box with no LLM provider key —
but listing LOCAL session metadata must never require a provider key.

Two layers of coverage:

* end-to-end subprocess runs (mirroring the ``tests/install/`` isolation
  pattern: scratch HOME, scratch CWD, an environment with NO provider key
  variables at all) proving ``polyrob session list`` exits 0 keyless and
  actually lists on-disk sessions;
* fast in-process CliRunner checks that the read-only verbs (``list``,
  ``show``, ``costs``, ``export``) skip the key preflight while a mutating
  verb (``cancel``) still runs it.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The canonical first line of ``modules.llm.profiles.no_key_message`` — the
#: string the old preflight printed before exiting 1.
NO_KEY_MARKER = "No usable provider credential"


def _keyless_env(tmp_home: Path) -> dict:
    """A from-scratch environment with NO provider key variables at all.

    Building the env dict from scratch (rather than deleting known key names)
    guarantees every provider key — OPENAI_API_KEY, ANTHROPIC_API_KEY,
    GEMINI_API_KEY, OPENROUTER_API_KEY, DEEPSEEK_API_KEY, and any future one —
    is absent. Mirrors ``tests/install/test_no_pollution.py::_clean_env``.
    """
    return {
        "HOME": str(tmp_home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(REPO_ROOT),
        "TERM": "dumb",
        "CI": "1",  # never prompt
    }


def _run_session_cli(args, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    argv = ["polyrob", "session"] + list(args)
    return subprocess.run(
        [sys.executable, "-c",
         f"import sys; sys.argv={argv!r}; from cli.polyrob import main; main()"],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=300,
    )


def test_session_list_keyless_empty_box_exits_zero(tmp_path):
    """Zero keys + zero sessions: `session list` exits 0 with an empty listing,
    never the no-key onboarding error."""
    cwd = tmp_path / "work"
    home = tmp_path / "home"
    cwd.mkdir()
    home.mkdir()

    proc = _run_session_cli(["list"], cwd=cwd, env=_keyless_env(home))

    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]
    assert "No sessions found" in proc.stdout, proc.stdout[-2000:]
    combined = proc.stdout + proc.stderr
    assert NO_KEY_MARKER not in combined, (
        "keyless `session list` must not print the no-key onboarding error:\n"
        + combined[-2000:]
    )


def test_session_list_keyless_lists_on_disk_session(tmp_path):
    """Zero keys + one session on disk: the listing actually shows it."""
    cwd = tmp_path / "work"
    home = tmp_path / "home"
    cwd.mkdir()
    home.mkdir()
    # Seed a session where the CLI container's PathManager roots the session
    # tree (POLYROB_DATA_DIR unset -> cwd/.polyrob/sessions), NEW flat layout
    # <user>/<session>/metadata.json.
    sess_dir = cwd / ".polyrob" / "sessions" / "local" / "sess_keyless_a1"
    sess_dir.mkdir(parents=True)
    (sess_dir / "metadata.json").write_text(json.dumps({
        "id": "sess_keyless_a1",
        "user_id": "local",
        "status": "created",
        "task": "keyless demo task",
        "created_at": "2026-08-27T00:00:00",
    }))

    proc = _run_session_cli(["list"], cwd=cwd, env=_keyless_env(home))

    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]
    # `session list` renders the id truncated to 10 chars.
    assert "sess_keyle" in proc.stdout, proc.stdout[-2000:]
    assert NO_KEY_MARKER not in (proc.stdout + proc.stderr)


# ---------------------------------------------------------------------------
# Fast in-process preflight-bypass checks (container build mocked).
# ---------------------------------------------------------------------------

def _fake_container():
    agent = MagicMock()
    agent.session_manager.get_all_sessions.return_value = []
    agent.session_manager.get_session_info.return_value = {
        "id": "abc", "status": "completed", "task": "t", "created_at": "t0",
    }
    agent.cancel_session = AsyncMock(return_value=True)
    container = MagicMock()
    container.get_agent.return_value = agent
    return container


def _patch_rig(monkeypatch, tmp_path):
    """Patch preflight (recording), container build, and pm() (empty tmp root —
    never let a test instantiate the real PathManager, which mkdirs)."""
    calls = []

    def _preflight(**kwargs):
        calls.append(kwargs)
        return True

    async def _fake_build(*args, **kwargs):
        return _fake_container()

    monkeypatch.setattr("cli.keys.preflight_or_onboard", _preflight)
    monkeypatch.setattr("core.bootstrap.build_cli_container", _fake_build)
    fake_pm = MagicMock()
    fake_pm.data_root = tmp_path
    monkeypatch.setattr("agents.task.path.pm", lambda: fake_pm)
    return calls


@pytest.mark.parametrize("argv", [
    ["list"],
    ["show", "abc"],
    ["costs", "abc"],
    ["export", "abc"],
])
def test_readonly_session_verbs_skip_key_preflight(monkeypatch, tmp_path, argv):
    from cli.commands.session import session

    calls = _patch_rig(monkeypatch, tmp_path)
    runner = CliRunner()
    runner.invoke(session, argv)

    assert calls == [], (
        f"read-only `session {argv[0]}` must not run the LLM key preflight"
    )


def test_cancel_is_keyless_and_routes_to_live_control(monkeypatch, tmp_path):
    from cli.commands.session import session

    calls = _patch_rig(monkeypatch, tmp_path)
    runner = CliRunner()
    control = AsyncMock()
    monkeypatch.setattr("cli.commands._session_control.control_live_session", control)
    result = runner.invoke(session, ["cancel", "abc"])

    assert calls == [], "Stopping work must not require an LLM credential"
    control.assert_awaited_once()
    assert result.exit_code == 0, result.output
