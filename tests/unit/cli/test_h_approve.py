"""Tests for the ``/approve`` REPL slash-command handler
(cli/ui/commands/h_approve.py) — owner-UX P2 T4.

``cmd_approve(ctx, args) -> str`` is driven directly as a function (the
established ``h_*`` pattern — see ``test_h_config.py``) via a tiny
``ApproveCtx`` dataclass so the handler is testable without a live REPL.
"""
import pytest

import tools.controller.approval as approval
from core.prefs import load_preferences, list_pending_pref_changes, write_preference


@pytest.fixture(autouse=True)
def _clean_approval_env(monkeypatch):
    """Isolate the module-level frozen-approval snapshots (WS-7 import-time
    freeze) so tests that set APPROVAL_REQUIRED_TOOLS/APPROVAL_PROVIDER via
    monkeypatch don't leak into/out of other tests."""
    for k in ("AGENT_COMPUTE_POSTURE", "APPROVAL_REQUIRED_TOOLS", "APPROVAL_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    import agents.task.constants as constants
    constants._refreeze_compute_posture_for_tests()
    approval._refreeze_approval_flags_for_tests()
    yield
    constants._refreeze_compute_posture_for_tests()
    approval._refreeze_approval_flags_for_tests()


def _ctx(tmp_path, user_id="u1"):
    from cli.ui.commands.h_approve import ApproveCtx
    return ApproveCtx(user_id=user_id, home_dir=tmp_path)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_empty_is_honest(tmp_path):
    from cli.ui.commands.h_approve import cmd_approve
    out = cmd_approve(_ctx(tmp_path), ["list"])
    assert "no approval gates" in out.lower()
    assert "provider: auto" in out
    assert "⚠" not in out  # no gates -> no warning


def test_list_default_is_list(tmp_path):
    from cli.ui.commands.h_approve import cmd_approve
    assert cmd_approve(_ctx(tmp_path), []) == cmd_approve(_ctx(tmp_path), ["list"])


def test_list_shows_env_frozen_source(tmp_path, monkeypatch):
    monkeypatch.setenv("APPROVAL_REQUIRED_TOOLS", "git_push")
    approval._refreeze_approval_flags_for_tests()
    from cli.ui.commands.h_approve import cmd_approve
    out = cmd_approve(_ctx(tmp_path), ["list"])
    assert "git_push" in out
    assert "env (frozen)" in out


def test_list_shows_pref_source_and_provider(tmp_path):
    write_preference(tmp_path, "u1", "approvals.require", ["custom_action"])
    from cli.ui.commands.h_approve import cmd_approve
    out = cmd_approve(_ctx(tmp_path), ["list"])
    assert "custom_action" in out
    assert "(pref)" in out
    assert "provider: auto" in out


def test_list_warns_when_provider_auto_and_gates_exist(tmp_path):
    write_preference(tmp_path, "u1", "approvals.require", ["custom_action"])
    from cli.ui.commands.h_approve import cmd_approve
    out = cmd_approve(_ctx(tmp_path), ["list"])
    assert "⚠" in out
    assert "auto" in out.lower()


def test_list_no_warning_when_provider_not_auto(tmp_path):
    write_preference(tmp_path, "u1", "approvals.require", ["custom_action"])
    write_preference(tmp_path, "u1", "approvals.provider", "deny")
    from cli.ui.commands.h_approve import cmd_approve
    out = cmd_approve(_ctx(tmp_path), ["list"])
    assert "⚠" not in out


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


def test_add_writes_pref(tmp_path):
    from cli.ui.commands.h_approve import cmd_approve
    out = cmd_approve(_ctx(tmp_path), ["add", "shell_run"])
    assert "shell_run" in out
    assert load_preferences(tmp_path, "u1")["approvals.require"] == ["shell_run"]


def test_add_dedupes(tmp_path):
    from cli.ui.commands.h_approve import cmd_approve
    cmd_approve(_ctx(tmp_path), ["add", "shell_run"])
    out = cmd_approve(_ctx(tmp_path), ["add", "shell_run"])
    assert "already" in out.lower()
    assert load_preferences(tmp_path, "u1")["approvals.require"] == ["shell_run"]


def test_add_unions_with_existing(tmp_path):
    from cli.ui.commands.h_approve import cmd_approve
    cmd_approve(_ctx(tmp_path), ["add", "shell_run"])
    cmd_approve(_ctx(tmp_path), ["add", "git_push"])
    assert set(load_preferences(tmp_path, "u1")["approvals.require"]) == {"shell_run", "git_push"}


def test_add_unknown_action_accepted_with_warning(tmp_path):
    """The CLI has no live tool registry to validate against — any non-empty
    token is accepted, but the reply is honest about the skipped check."""
    from cli.ui.commands.h_approve import cmd_approve
    out = cmd_approve(_ctx(tmp_path), ["add", "totally_made_up_action"])
    assert "totally_made_up_action" in out
    assert "registry" in out.lower()
    assert "skipped" in out.lower()
    assert load_preferences(tmp_path, "u1")["approvals.require"] == ["totally_made_up_action"]


def test_add_missing_arg_usage(tmp_path):
    from cli.ui.commands.h_approve import cmd_approve
    out = cmd_approve(_ctx(tmp_path), ["add"])
    assert "usage" in out.lower()


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def test_remove_pref_entry_queues_proposal_not_immediate_write(tmp_path):
    write_preference(tmp_path, "u1", "approvals.require", ["custom_action", "other_action"])
    from cli.ui.commands.h_approve import cmd_approve
    out = cmd_approve(_ctx(tmp_path), ["remove", "custom_action"])
    assert "queued" in out.lower()
    assert "/pending approve" in out
    # NOT immediately rewritten — the pref file still has BOTH entries.
    assert load_preferences(tmp_path, "u1")["approvals.require"] == ["custom_action", "other_action"]
    # A pending pref-change proposal now exists for the removal OPERATION.
    pending = list_pending_pref_changes("u1", tmp_path)
    assert any(p["id"] == "approvals.require" for p in pending)


def test_remove_queues_operation_not_snapshot(tmp_path):
    """P2 T4 review fix — the reviewer's exact scenario: queue remove A, then
    /approve add C BEFORE promoting; the promote must apply against the CURRENT
    list (C survives, A gone) — a stale full-list snapshot would erase C."""
    from core import self_evolution as se
    from core.instance import DEFAULT_INSTANCE_ID
    from cli.ui.commands.h_approve import cmd_approve

    write_preference(tmp_path, "u1", "approvals.require", ["A", "B"])
    out = cmd_approve(_ctx(tmp_path), ["remove", "A"])
    assert "queued" in out.lower()
    # The pending preview renders the OPERATION, not a raw list snapshot.
    pending = list_pending_pref_changes("u1", tmp_path)
    assert any("remove 'A' from approvals.require" in p["preview"] for p in pending)

    # Owner adds C while the removal is still pending.
    cmd_approve(_ctx(tmp_path), ["add", "C"])

    ok, _msg = se.promote(se.KIND_PREF_CHANGE, "approvals.require", user_id="u1",
                          home_dir=tmp_path, instance_id=DEFAULT_INSTANCE_ID)
    assert ok
    got = load_preferences(tmp_path, "u1")["approvals.require"]
    assert "C" in got and "B" in got and "A" not in got


def test_remove_env_entry_explains_operator_control(tmp_path, monkeypatch):
    monkeypatch.setenv("APPROVAL_REQUIRED_TOOLS", "git_push")
    approval._refreeze_approval_flags_for_tests()
    from cli.ui.commands.h_approve import cmd_approve
    out = cmd_approve(_ctx(tmp_path), ["remove", "git_push"])
    assert "operator-controlled" in out.lower()
    assert "APPROVAL_REQUIRED_TOOLS" in out


def test_remove_posture_entry_explains_posture_control(tmp_path, monkeypatch):
    """A posture-sourced gate (compute posture >= 2 union) must name the
    posture mechanism, not claim it was 'set in env'."""
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "2")
    import agents.task.constants as constants
    constants._refreeze_compute_posture_for_tests()
    from cli.ui.commands.h_approve import cmd_approve
    out = cmd_approve(_ctx(tmp_path), ["remove", "shell_run"])
    assert "operator-controlled" in out.lower()
    assert "AGENT_COMPUTE_POSTURE" in out


def test_remove_not_gated_action(tmp_path):
    from cli.ui.commands.h_approve import cmd_approve
    out = cmd_approve(_ctx(tmp_path), ["remove", "never_gated"])
    assert "not currently gated" in out.lower()


def test_remove_missing_arg_usage(tmp_path):
    from cli.ui.commands.h_approve import cmd_approve
    out = cmd_approve(_ctx(tmp_path), ["remove"])
    assert "usage" in out.lower()


# ---------------------------------------------------------------------------
# unknown subcommand
# ---------------------------------------------------------------------------


def test_unknown_subcommand(tmp_path):
    from cli.ui.commands.h_approve import cmd_approve
    out = cmd_approve(_ctx(tmp_path), ["bogus"])
    assert "unknown" in out.lower()
    assert "usage" in out.lower()
