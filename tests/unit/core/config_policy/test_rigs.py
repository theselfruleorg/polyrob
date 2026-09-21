"""057 WS-A: named tool rigs for autonomous sessions."""
import pytest

from core.config_policy.rigs import (
    FULL_RIG, RIGS, default_rig_name, is_rig, resolve_rig_tools, rig_names,
    rig_tools,
)


def test_every_rig_has_a_fixed_order_and_no_duplicates():
    for name, ids in RIGS.items():
        assert isinstance(ids, tuple), f"{name} must be an ordered tuple"
        assert len(set(ids)) == len(ids), f"{name} repeats an id"
        assert ids, f"{name} is empty"


def test_every_rig_can_speak_to_its_owner():
    # A rig that cannot `message` runs, finishes, and tells nobody.
    for name, ids in RIGS.items():
        assert "message" in ids, f"{name} has no way to reach the owner"


def test_full_is_a_name_that_means_the_callers_default():
    assert FULL_RIG in rig_names()
    assert rig_tools(FULL_RIG) is None
    assert is_rig("full") and is_rig("ops") and not is_rig("nope")


def test_unknown_rig_is_fail_open(caplog):
    assert rig_tools("typo-rig") is None


def test_default_is_full_so_behaviour_is_unchanged(monkeypatch):
    monkeypatch.delenv("AUTONOMOUS_RIG_DEFAULT", raising=False)
    assert default_rig_name() == FULL_RIG
    assert resolve_rig_tools({}, ["filesystem", "task"]) == ["filesystem", "task"]
    assert resolve_rig_tools({}, None) is None


def test_payload_tools_win_verbatim(monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_RIG_DEFAULT", "ops")
    payload = {"tools": ["defi_trade", "filesystem"], "rig": "research"}
    assert resolve_rig_tools(payload, ["x"]) == ["defi_trade", "filesystem"]


def test_payload_rig_beats_the_env_default(monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_RIG_DEFAULT", "ops")
    assert resolve_rig_tools({"rig": "research"}, ["x"]) == list(RIGS["research"])


def test_env_default_applies_when_the_payload_says_nothing(monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_RIG_DEFAULT", "ops")
    assert resolve_rig_tools({}, ["x"]) == list(RIGS["ops"])
    assert resolve_rig_tools(None, ["x"]) == list(RIGS["ops"])


def test_payload_rig_full_returns_the_callers_default(monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_RIG_DEFAULT", "ops")
    # An explicit `rig=full` on the row means "this job wants everything",
    # even on a deploy whose default rig is narrow.
    assert resolve_rig_tools({"rig": "full"}, ["x", "y"]) == ["x", "y"]


def test_unknown_env_rig_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_RIG_DEFAULT", "does-not-exist")
    assert resolve_rig_tools({}, ["x"]) == ["x"]


def test_resolution_is_stable_across_calls(monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_RIG_DEFAULT", "social")
    first = resolve_rig_tools({}, None)
    second = resolve_rig_tools({}, None)
    assert first == second == list(RIGS["social"])


def test_returned_list_is_a_copy(monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_RIG_DEFAULT", "ops")
    got = resolve_rig_tools({}, None)
    got.append("mutated")
    assert "mutated" not in RIGS["ops"]


def test_unknown_payload_rig_is_named_not_silent(monkeypatch, caplog):
    import logging
    monkeypatch.delenv("AUTONOMOUS_RIG_DEFAULT", raising=False)
    with caplog.at_level(logging.WARNING, logger="core.config_policy.rigs"):
        assert resolve_rig_tools({"rig": "reserch"}, ["x"]) == ["x"]
    assert "reserch" in caplog.text
