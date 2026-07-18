"""Pref-added approval gates: union-only, frozen env untouched (spec §3.4)."""
from core.prefs import write_preference
from tools.controller.approval import pref_gated_actions, frozen_approval_required_tools


def test_pref_adds_gated_action(tmp_path):
    write_preference(tmp_path, "u1", "approvals.require", ["git_push"])
    assert "git_push" in pref_gated_actions("u1", tmp_path)


def test_pref_cannot_remove_env_gate(tmp_path):
    """The frozen env snapshot is independent of any pref content."""
    frozen_before = frozen_approval_required_tools()
    write_preference(tmp_path, "u1", "approvals.require", [])
    assert frozen_approval_required_tools() == frozen_before
    assert pref_gated_actions("u1", tmp_path) == frozenset()


def test_no_prefs_is_empty(tmp_path):
    assert pref_gated_actions("u1", tmp_path) == frozenset()
