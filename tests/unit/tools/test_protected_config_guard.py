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
    ".polyrob/wallet/meta.json",
    ".polyrob/wallet/audit.jsonl",
    "/var/lib/polyrob/wallet/meta.json",
    "data/wallet/audit.jsonl",
])
def test_wallet_policy_files_are_credential_guarded(path):
    """H3: the wallet derivation record and spend audit are money-policy state.
    Under POLYROB_LOCAL the workspace IS the project cwd (which contains
    .polyrob/wallet/), so without this the agent's file tools could zero the
    audit sink (resetting the daily-cap window + replay guard on the next
    restart) or rewrite the write-once derivation scheme."""
    assert is_credential_file(Path(path)) is True


@pytest.mark.parametrize("path", [
    ".polyrob/wallet/audit.jsonl.hwm",
    "/var/lib/polyrob/wallet/audit.jsonl.hwm",
    "data/wallet/audit.jsonl.hwm",
])
def test_wallet_audit_hwm_sidecar_is_credential_guarded(path):
    """Minor #6 (2026-07-16): `JsonlAuditSink` writes a `<path>.hwm` high-water-mark
    sidecar next to `audit.jsonl` (core/wallet/audit_sink.py) to detect truncation.
    Same money-policy-state rationale as the audit log itself — the exact-match
    `wallet/audit.jsonl` glob alone did not cover this sidecar filename."""
    assert is_credential_file(Path(path)) is True


@pytest.mark.parametrize("path", [
    # M2: `cli/update/snapshot.py` stores config copies under a numeric-index
    # prefix (`config/{i:02d}_{basename}`), which defeated the `.env*`/`*.env`/
    # `config/.env.*` globs (they all assume the ORIGINAL basename). A local
    # box's `config/.env.production` (holds MASTER_SEED) would otherwise be
    # `read_file`-able once backed up by `polyrob update`.
    "config/00_.env.production",
    "snapshots/20260715T120000Z_0.5.1/config/00_.env.production",
    "/data/snapshots/20260715T120000Z_0.5.1/config/01_.env.development",
    # M2 (directory analog, surfaced by the M1 fix): dir copies get the same
    # numeric-index prefix on the COPIED DIRECTORY name
    # (`dirs/{i:02d}_{src.name}`), so `wallet/meta.json` becomes
    # `dirs/00_wallet/meta.json` — the exact multi-component `wallet/meta.json`
    # glob no longer lines up. The blanket `snapshots/*/dirs/*` rule covers any
    # backed-up dir (wallet/identity/skills) regardless of the index prefix.
    "snapshots/20260715T120000Z_0.5.1/dirs/00_wallet/meta.json",
    "snapshots/20260715T120000Z_0.5.1/dirs/00_wallet/audit.jsonl",
])
def test_snapshot_renamed_copies_are_credential_guarded(path):
    assert is_credential_file(Path(path)) is True


@pytest.mark.parametrize("path", [
    "app.py",
    "README.md",
    "src/config.py",       # a python 'config' module is NOT a dotenv file
    "environment.md",
    "wallet.py",           # a python 'wallet' module is not the wallet data dir
    "cli/commands/wallet.py",
])
def test_ordinary_files_still_editable(path):
    assert is_credential_file(Path(path)) is False


def test_env_dotted_globs_are_intentionally_broad():
    """M2's `*.env.*` glob denies ANY basename containing `.env.` (e.g. a
    numeric-prefixed snapshot copy `00_.env.production`), which also catches
    non-secret names like `test.env.example`. This is a deliberate over-deny:
    the guard's job is to never let a real credential slip through a renamed
    copy, and refusing an example file's read/write is a minor inconvenience
    (not a security or data-loss issue) compared to a leaked MASTER_SEED."""
    assert is_credential_file(Path("test.env.example")) is True
    assert is_credential_file(Path("00_.env.production")) is True


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
