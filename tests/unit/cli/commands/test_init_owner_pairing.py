"""polyrob init pairs an owner + instance id into ~/.polyrob/.env."""
from click.testing import CliRunner

from cli.commands.init import init_cmd


def _env_after(tmp_path, monkeypatch, args):
    monkeypatch.setenv("HOME", str(tmp_path))
    # core_paths.polyrob_home() resolves from HOME; keep the CWD writable for .polyrob/sessions
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(init_cmd, args)
    assert r.exit_code == 0, r.output
    env_path = tmp_path / ".polyrob" / ".env"
    return env_path.read_text() if env_path.exists() else ""


def test_init_non_interactive_writes_owner_and_instance(tmp_path, monkeypatch):
    env = _env_after(tmp_path, monkeypatch,
                     ["--no-prompt", "--owner", "rob", "--instance-id", "rob",
                      "--openai-key", "sk-test"])
    assert "POLYROB_OWNER_USER_ID=rob" in env
    assert "POLYROB_INSTANCE_ID=rob" in env


def test_init_owner_flag_alone_backfills_instance(tmp_path, monkeypatch):
    env = _env_after(tmp_path, monkeypatch,
                     ["--no-prompt", "--owner", "alice", "--openai-key", "sk-test"])
    assert "POLYROB_OWNER_USER_ID=alice" in env
    assert "POLYROB_INSTANCE_ID=alice" in env  # backfilled from owner


def test_init_instance_flag_alone_backfills_owner(tmp_path, monkeypatch):
    env = _env_after(tmp_path, monkeypatch,
                     ["--no-prompt", "--instance-id", "bot7", "--openai-key", "sk-test"])
    assert "POLYROB_INSTANCE_ID=bot7" in env
    assert "POLYROB_OWNER_USER_ID=bot7" in env


def test_init_no_pairing_flags_writes_nothing_pairing(tmp_path, monkeypatch):
    env = _env_after(tmp_path, monkeypatch, ["--no-prompt", "--openai-key", "sk-test"])
    # no owner/instance flags in non-interactive => not written (falsy skipped)
    assert "POLYROB_OWNER_USER_ID" not in env
    assert "POLYROB_INSTANCE_ID" not in env
