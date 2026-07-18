"""Tests for `polyrob approvals` (cli/commands/approvals.py) — owner-UX P2 T4.

The CLI counterpart of the `/approve` REPL command
(cli/ui/commands/h_approve.py) — same three verbs over the same
``effective_approval_state()`` helper. Uses the hidden ``--home`` option
(test/ops only) to point at a tmp_path preferences tree, mirroring
``test_config_cmd.py``'s pattern for ``polyrob config``.
"""
import pytest
from click.testing import CliRunner

import tools.controller.approval as approval
from core.prefs import list_pending_pref_changes, load_preferences, write_preference


@pytest.fixture(autouse=True)
def _clean_approval_env(monkeypatch):
    for k in ("AGENT_COMPUTE_POSTURE", "APPROVAL_REQUIRED_TOOLS", "APPROVAL_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    import agents.task.constants as constants
    constants._refreeze_compute_posture_for_tests()
    approval._refreeze_approval_flags_for_tests()
    yield
    constants._refreeze_compute_posture_for_tests()
    approval._refreeze_approval_flags_for_tests()


def _invoke(args):
    from cli.commands.approvals import approvals as approvals_group
    return CliRunner().invoke(approvals_group, args)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_empty(tmp_path):
    res = _invoke(["list", "--user", "u1", "--home", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "no approval gates" in res.output.lower()
    assert "provider: auto" in res.output


def test_list_shows_env_frozen_and_pref_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("APPROVAL_REQUIRED_TOOLS", "git_push")
    approval._refreeze_approval_flags_for_tests()
    write_preference(tmp_path, "u1", "approvals.require", ["custom_action"])
    res = _invoke(["list", "--user", "u1", "--home", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "git_push" in res.output and "env (frozen)" in res.output
    assert "custom_action" in res.output and "(pref)" in res.output


def test_list_warns_when_auto_and_gated(tmp_path):
    write_preference(tmp_path, "u1", "approvals.require", ["custom_action"])
    res = _invoke(["list", "--user", "u1", "--home", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "⚠" in res.output


def test_list_no_warning_when_no_gates(tmp_path):
    res = _invoke(["list", "--user", "u1", "--home", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "⚠" not in res.output


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


def test_add_writes_pref(tmp_path):
    res = _invoke(["add", "shell_run", "--user", "u1", "--home", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert load_preferences(tmp_path, "u1")["approvals.require"] == ["shell_run"]


def test_add_dedupes(tmp_path):
    _invoke(["add", "shell_run", "--user", "u1", "--home", str(tmp_path)])
    res = _invoke(["add", "shell_run", "--user", "u1", "--home", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "already" in res.output.lower()
    assert load_preferences(tmp_path, "u1")["approvals.require"] == ["shell_run"]


def test_add_unknown_action_accepted_with_warning(tmp_path):
    res = _invoke(["add", "totally_made_up_action", "--user", "u1", "--home", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "registry" in res.output.lower() and "skipped" in res.output.lower()
    assert load_preferences(tmp_path, "u1")["approvals.require"] == ["totally_made_up_action"]


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def test_remove_pref_entry_queues_not_immediate(tmp_path):
    write_preference(tmp_path, "u1", "approvals.require", ["custom_action", "other_action"])
    res = _invoke(["remove", "custom_action", "--user", "u1", "--home", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "queued" in res.output.lower()
    assert "owner promote" in res.output
    assert load_preferences(tmp_path, "u1")["approvals.require"] == ["custom_action", "other_action"]
    pending = list_pending_pref_changes("u1", tmp_path)
    assert any(p["id"] == "approvals.require" for p in pending)


def test_remove_queues_operation_not_snapshot(tmp_path):
    """P2 T4 review fix: remove A queued, C added before promote -> promote
    applies against the CURRENT list; C survives, A gone."""
    from core import self_evolution as se
    from core.instance import DEFAULT_INSTANCE_ID

    write_preference(tmp_path, "u1", "approvals.require", ["A", "B"])
    res = _invoke(["remove", "A", "--user", "u1", "--home", str(tmp_path)])
    assert res.exit_code == 0, res.output
    pending = list_pending_pref_changes("u1", tmp_path)
    assert any("remove 'A' from approvals.require" in p["preview"] for p in pending)

    res = _invoke(["add", "C", "--user", "u1", "--home", str(tmp_path)])
    assert res.exit_code == 0, res.output

    ok, _msg = se.promote(se.KIND_PREF_CHANGE, "approvals.require", user_id="u1",
                          home_dir=tmp_path, instance_id=DEFAULT_INSTANCE_ID)
    assert ok
    got = load_preferences(tmp_path, "u1")["approvals.require"]
    assert "C" in got and "B" in got and "A" not in got


def test_remove_env_entry_explains_operator_control(tmp_path, monkeypatch):
    monkeypatch.setenv("APPROVAL_REQUIRED_TOOLS", "git_push")
    approval._refreeze_approval_flags_for_tests()
    res = _invoke(["remove", "git_push", "--user", "u1", "--home", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "operator-controlled" in res.output.lower()
    assert "APPROVAL_REQUIRED_TOOLS" in res.output


def test_remove_posture_entry_explains_posture_control(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "2")
    import agents.task.constants as constants
    constants._refreeze_compute_posture_for_tests()
    res = _invoke(["remove", "shell_run", "--user", "u1", "--home", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "operator-controlled" in res.output.lower()
    assert "AGENT_COMPUTE_POSTURE" in res.output


def test_remove_not_gated(tmp_path):
    res = _invoke(["remove", "never_gated", "--user", "u1", "--home", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "not currently gated" in res.output.lower()


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def test_approvals_group_registered_on_polyrob_cli():
    from cli.polyrob import cli
    assert "approvals" in cli.commands
