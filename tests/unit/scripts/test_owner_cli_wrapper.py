"""`scripts/owner_cli.sh` runs an owner CLI verb against the DEPLOYED home as the
service user, with the deployment's identity passed explicitly.

The shape it protects: 057 WS-G refuses a root euid on the deployed home, and the
service account cannot read /etc/polyrob/polyrob.env, so the CLI cannot find
POLYROB_OWNER_USER_ID and deliberately refuses to guess a tenant. These tests pin
the two things that make the wrapper safe rather than convenient — it passes ONLY
the three identity variables (never the secrets sitting in the same file), and a
missing owner id is a refusal naming the remedy, never a guess.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "owner_cli.sh"


def _run(env_file: Path, *args, extra_env=None):
    env = dict(os.environ)
    env.update({
        "POLYROB_ENV_FILE": str(env_file),
        "POLYROB_CLI_BIN": "/opt/polyrob/venv/bin/polyrob",
        "POLYROB_SVC_USER": "polyrob-agent",
        "POLYROB_SUDO": "sudo",
    })
    env.update(extra_env or {})
    return subprocess.run(["bash", str(SCRIPT), *args],
                          capture_output=True, text=True, env=env)


def _env_file(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "polyrob.env"
    p.write_text(body, encoding="utf-8")
    return p


def test_script_exists_and_is_executable():
    assert SCRIPT.exists()
    assert os.access(SCRIPT, os.X_OK), "owner_cli.sh must be chmod +x to be usable"


def test_builds_the_command_with_the_three_identity_vars(tmp_path):
    env_file = _env_file(tmp_path, (
        "POLYROB_DATA_DIR=/var/lib/polyrob\n"
        "POLYROB_INSTANCE_ID=rob\n"
        "POLYROB_OWNER_USER_ID=rob\n"
        "OPENROUTER_API_KEY=sk-or-v1-NOT-A-REAL-KEY\n"
    ))
    r = _run(env_file, "--print", "owner", "asks")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "sudo -u polyrob-agent env" in out
    assert "POLYROB_DATA_DIR=/var/lib/polyrob" in out
    assert "POLYROB_OWNER_USER_ID=rob" in out
    assert "POLYROB_INSTANCE_ID=rob" in out
    assert out.rstrip().endswith("/opt/polyrob/venv/bin/polyrob owner asks")


def test_secrets_in_the_env_file_never_reach_the_child(tmp_path):
    """The wrapper reads a file full of credentials. The child gets three vars."""
    env_file = _env_file(tmp_path, (
        "POLYROB_DATA_DIR=/var/lib/polyrob\n"
        "POLYROB_OWNER_USER_ID=rob\n"
        "OPENROUTER_API_KEY=sk-or-v1-NOT-A-REAL-KEY\n"
        "WALLET_MNEMONIC='never in a child env'\n"
    ))
    r = _run(env_file, "--print", "owner", "asks")
    assert r.returncode == 0, r.stderr
    assert "sk-or-v1" not in r.stdout
    assert "MNEMONIC" not in r.stdout
    # and nothing leaked to stderr either
    assert "sk-or-v1" not in r.stderr


def test_missing_owner_id_refuses_and_names_the_remedy(tmp_path):
    env_file = _env_file(tmp_path, "POLYROB_DATA_DIR=/var/lib/polyrob\n")
    r = _run(env_file, "--print", "owner", "asks")
    assert r.returncode != 0
    assert "POLYROB_OWNER_USER_ID" in r.stderr
    assert "refusing to guess" in r.stderr
    # a refusal, not a silent default tenant
    assert "polyrob owner asks" not in r.stdout


def test_missing_data_dir_refuses(tmp_path):
    env_file = _env_file(tmp_path, "POLYROB_OWNER_USER_ID=rob\n")
    r = _run(env_file, "--print", "owner", "asks")
    assert r.returncode != 0
    assert "POLYROB_DATA_DIR" in r.stderr


def test_unreadable_env_file_says_so(tmp_path):
    r = _run(tmp_path / "nope.env", "--print", "owner", "asks")
    assert r.returncode != 0
    assert "cannot read" in r.stderr


def test_no_args_prints_usage(tmp_path):
    env_file = _env_file(tmp_path, "POLYROB_DATA_DIR=/x\nPOLYROB_OWNER_USER_ID=rob\n")
    r = _run(env_file)
    assert r.returncode == 64
    assert "usage:" in r.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_script_parses_under_bash():
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
