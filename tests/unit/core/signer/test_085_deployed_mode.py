"""085 W8 (signer L-2 follow-up): after the cut-over WALLET_SIGNER=remote lives
only in /etc/polyrob/agent-signer.env. The root CLI on the box has no
WALLET_SIGNER in its shell, yet `polyrob owner pending` must still list the
signer's approvals. One reader of polyrob.env + agent-signer.env serves the
listing, the deploy and the wallet continuity check."""
import os

import pytest

import core.signer as dm
from core.signer import deployed_mode as entry


def _files(tmp_path, shared=None, agent=None):
    s, a = tmp_path / "polyrob.env", tmp_path / "agent-signer.env"
    if shared is not None:
        s.write_text(shared)
    if agent is not None:
        a.write_text(agent)
    return str(s), str(a)


def test_the_agent_file_wins_over_the_shared_file(tmp_path):
    s, a = _files(tmp_path, "WALLET_SIGNER=shadow\n", "# 085 W8\nWALLET_SIGNER=remote\n")
    assert dm.declared_mode(s, a) == "remote"


def test_after_the_cutover_the_shared_file_has_no_mode(tmp_path):
    s, a = _files(tmp_path, "OTHER=1\n", 'WALLET_SIGNER="Remote"\n')
    assert dm.declared_mode(s, a) == "remote"


def test_shadow_from_the_shared_file_and_none_when_undeclared(tmp_path):
    s, a = _files(tmp_path, "WALLET_SIGNER=shadow\n")
    assert dm.declared_mode(s, a) == "shadow"
    s, _ = _files(tmp_path, "OTHER=1\n")
    assert dm.declared_mode(s, str(tmp_path / "absent.env")) is None


def test_an_unknown_value_reads_local(tmp_path):
    s, a = _files(tmp_path, "", "WALLET_SIGNER=bogus\n")
    assert dm.declared_mode(s, a) == "local"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0600 file")
def test_an_unreadable_agent_file_declares_nothing(tmp_path):
    s, a = _files(tmp_path, "OTHER=1\n", "WALLET_SIGNER=remote\n")
    os.chmod(a, 0)
    try:
        assert dm.declared_mode(s, a) is None
    finally:
        os.chmod(a, 0o600)


def test_the_process_env_wins_then_the_deployment(tmp_path, monkeypatch):
    s, a = _files(tmp_path, "", "WALLET_SIGNER=remote\n")
    monkeypatch.setattr(dm, "SHARED_ENV_FILE", s)
    monkeypatch.setattr(dm, "AGENT_SIGNER_ENV_FILE", a)
    monkeypatch.delenv("WALLET_SIGNER", raising=False)
    assert dm.agent_signer_mode() == "remote"
    monkeypatch.setenv("WALLET_SIGNER", "shadow")
    assert dm.agent_signer_mode() == "shadow"


def test_signer_mode_itself_never_reads_the_files(tmp_path, monkeypatch):
    """x402-api / health / webview stay signer-free: only the listing reads them."""
    from core.signer import signer_mode
    s, a = _files(tmp_path, "", "WALLET_SIGNER=remote\n")
    monkeypatch.setattr(dm, "SHARED_ENV_FILE", s)
    monkeypatch.setattr(dm, "AGENT_SIGNER_ENV_FILE", a)
    monkeypatch.delenv("WALLET_SIGNER", raising=False)
    assert signer_mode() == "local"


def test_main_prints_the_mode_or_nothing(tmp_path, capsys):
    s, a = _files(tmp_path, "", "WALLET_SIGNER=remote\n")
    assert entry.main([s, a]) == 0
    assert capsys.readouterr().out.strip() == "remote"
    assert entry.main([s, str(tmp_path / "absent.env")]) == 0
    assert capsys.readouterr().out.strip() == ""


def test_root_owner_pending_lists_signer_approvals_without_the_shell_flag(tmp_path, monkeypatch):
    import core.signer.approvals as approvals
    from tools.controller.approval_queue import all_pending
    s, a = _files(tmp_path, "OTHER=1\n", "WALLET_SIGNER=remote\n")
    monkeypatch.setattr(dm, "SHARED_ENV_FILE", s)
    monkeypatch.setattr(dm, "AGENT_SIGNER_ENV_FILE", a)
    monkeypatch.delenv("WALLET_SIGNER", raising=False)
    item = {"kind": "signer_approval", "id": "sig-0123456789", "chars": 1,
            "preview": "signer review", "tool": "signer.x402_authorize"}
    monkeypatch.setattr(approvals, "pending_items", lambda client=None: [item])
    got = all_pending(user_id="u1", home_dir=str(tmp_path), instance_id="i1")
    assert item in got.items
    # A local deployment (no file declares a signer) does not ask the signer.
    os.remove(a)
    monkeypatch.setattr(approvals, "pending_items",
                        lambda client=None: pytest.fail("must not ask the signer"))
    got = all_pending(user_id="u1", home_dir=str(tmp_path), instance_id="i1")
    assert item not in got.items
