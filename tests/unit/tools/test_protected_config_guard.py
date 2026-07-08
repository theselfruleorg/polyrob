"""WS-7: hard-deny env/config-file writes + freeze the security flags at import.

The agent must never be able to raise its OWN posture or flip approval gating by
writing the env/config file (config/.env.*, /etc/polyrob/polyrob.env, the mcp-token
store). The prod env file's basename `polyrob.env` did NOT match the existing `.env*`
glob — that gap is the escalation path this closes. Plus the approval flags are frozen
at import so a mid-process env mutation can't flip gating.
"""
from pathlib import Path

import pytest

from agents.task.agent.core.secret_guard import is_credential_file, is_protected_config_path


# --- protected env/config file names (the WS-7 gap) ------------------------------

@pytest.mark.parametrize("path", [
    "polyrob.env",
    "/etc/polyrob/polyrob.env",
    "config/.env.production",
    "config/.env.development",
    "prod.env",
    "/opt/polyrob/config/.env.production",
    ".env",
    ".env.local",
])
def test_env_and_config_files_are_credential_guarded(path):
    assert is_credential_file(Path(path)) is True


@pytest.mark.parametrize("path", [
    "app.py",
    "README.md",
    "src/config.py",       # a python 'config' module is NOT a dotenv file
    "environment.md",
])
def test_ordinary_files_still_editable(path):
    assert is_credential_file(Path(path)) is False


# --- absolute protected-config paths for the self_env tool (WS-5) -----------------

@pytest.mark.parametrize("path", [
    "/etc/polyrob/polyrob.env",
    "/etc/polyrob/anything.conf",
    "/etc/rob/rob.env",
])
def test_system_config_dirs_are_protected(path):
    assert is_protected_config_path(Path(path)) is True


@pytest.mark.parametrize("path", [
    "/opt/polyrob/agents/task/service.py",
    "/opt/polyrob/README.md",
])
def test_install_tree_source_is_not_protected_config(path):
    # source files under the install tree are patchable by self_env (WS-5);
    # only the env/config files are hard-denied.
    assert is_protected_config_path(Path(path)) is False


# --- filesystem tool refuses the env file ----------------------------------------

def test_filesystem_secret_guard_refuses_polyrob_env():
    from agents.task.agent.core.secret_guard import is_credential_file
    # the guard the filesystem/coding write path calls must now catch polyrob.env
    assert is_credential_file(Path("/some/workspace/polyrob.env")) is True


# --- frozen approval flags -------------------------------------------------------

def test_approval_flags_frozen_at_import(monkeypatch):
    import tools.controller.approval as approval
    approval._refreeze_approval_flags_for_tests()  # baseline (no env)
    monkeypatch.setenv("APPROVAL_REQUIRED_TOOLS", "shell_run,self_env_install_dep")
    monkeypatch.setenv("APPROVAL_PROVIDER", "deny")
    # frozen snapshot must NOT reflect the post-import mutation
    assert approval.frozen_approval_required_tools() == frozenset()
    assert approval.frozen_approval_provider() == "auto"


def test_approval_flags_reflect_env_at_freeze_time(monkeypatch):
    import tools.controller.approval as approval
    monkeypatch.setenv("APPROVAL_REQUIRED_TOOLS", "shell_run, self_env_install_dep")
    monkeypatch.setenv("APPROVAL_PROVIDER", "interactive_cli")
    approval._refreeze_approval_flags_for_tests()
    try:
        assert approval.frozen_approval_required_tools() == {"shell_run", "self_env_install_dep"}
        assert approval.frozen_approval_provider() == "interactive_cli"
    finally:
        monkeypatch.delenv("APPROVAL_REQUIRED_TOOLS", raising=False)
        monkeypatch.delenv("APPROVAL_PROVIDER", raising=False)
        approval._refreeze_approval_flags_for_tests()
