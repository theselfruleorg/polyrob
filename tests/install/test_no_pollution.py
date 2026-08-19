"""Directory + first-run hygiene (proposal 027 WP5).

Read-only commands used to write into the user's CWD (`polyrob doctor` in an
empty dir left `.polyrob/logs/bot.log`), the REPL created `./.polyrob/sessions`
BEFORE the key gate, running from $HOME collapsed the data home into the config
home, `polyrob init` wrote a .gitignore even outside git repos, and telemetry
defaulted on without disclosure.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _clean_env(tmp_home: Path) -> dict:
    env = {
        "HOME": str(tmp_home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(REPO_ROOT),
        "TERM": "dumb",
        "CI": "1",  # never prompt
    }
    return env


def test_doctor_creates_nothing_in_cwd(tmp_path):
    cwd = tmp_path / "empty"
    home = tmp_path / "home"
    cwd.mkdir()
    home.mkdir()
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.argv=['polyrob','doctor']; "
         "from cli.polyrob import main; main()"],
        cwd=cwd, env=_clean_env(home), capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    leftovers = sorted(p.name for p in cwd.iterdir())
    assert leftovers == [], (
        f"read-only doctor must not write into the CWD; created: {leftovers}"
    )


def test_declined_zero_key_repl_leaves_cwd_empty(tmp_path):
    cwd = tmp_path / "empty"
    home = tmp_path / "home"
    cwd.mkdir()
    home.mkdir()
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.argv=['polyrob']; from cli.polyrob import main; main()"],
        cwd=cwd, env=_clean_env(home), capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 1  # zero-key non-TTY start is a failure (WP3)
    leftovers = sorted(p.name for p in cwd.iterdir())
    assert leftovers == [], (
        f"a declined zero-key start must leave the directory as found: {leftovers}"
    )


def test_home_cwd_collision_redirects_data_home(monkeypatch, tmp_path):
    home = tmp_path / "home" / ".polyrob"
    home.mkdir(parents=True)
    monkeypatch.setenv("POLYROB_HOME", str(home))
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    # Running from $HOME: cwd/.polyrob IS the config home.
    monkeypatch.chdir(home.parent)

    from core.runtime_paths import resolve_runtime_paths

    paths = resolve_runtime_paths(local=True)
    assert paths.data_home != home.resolve(), (
        "running from the config home must not mix .env/auth.json with the "
        "sidecar DBs"
    )
    assert paths.data_home == (home / "data").resolve()


def test_init_writes_no_gitignore_outside_git(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from cli.commands.init import init_cmd

    home = tmp_path / "home" / ".polyrob"
    home.mkdir(parents=True)
    monkeypatch.setattr("core.paths.polyrob_home", lambda: home)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as fs:
        result = runner.invoke(init_cmd, ["--no-prompt"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        assert not (Path(fs) / ".gitignore").exists(), (
            "init must not write .gitignore outside a git work tree"
        )


def test_telemetry_defaults_off(monkeypatch):
    monkeypatch.delenv("ANONYMIZED_TELEMETRY", raising=False)
    from agents.task.telemetry.service import ProductTelemetry

    telemetry = ProductTelemetry()
    assert telemetry.posthog_enabled is False, (
        "analytics must be opt-in (ANONYMIZED_TELEMETRY=true), never on by default"
    )
