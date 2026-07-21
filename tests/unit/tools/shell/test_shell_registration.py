"""WS-2: shell/process registration + delegation guardrails.

The tools must register ONLY when the compute posture makes them reachable, must be
delegation-blocked for leaves, and must never leak into a default or child toolset.
Registration is also the landmine check for the `from __future__ import annotations`
param-model trap (agent-upgrades-wave4): the actions' Pydantic param models must be
introspectable by the Registry.
"""
import pytest

import agents.task.constants as c


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("AGENT_COMPUTE_POSTURE", "SHELL_TOOLS_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    c._refreeze_compute_posture_for_tests()
    yield
    # LIFO landmine (see the docker-test twins + inbox 2026-07-14): this teardown
    # runs BEFORE monkeypatch reverts env, so refreezing first re-snapshots a
    # test's posture and leaks it into every later test in the process. Pop the
    # envs explicitly, THEN refreeze.
    import os as _os
    _os.environ.pop("AGENT_COMPUTE_POSTURE", None)
    _os.environ.pop("SHELL_TOOLS_ENABLED", None)
    c._refreeze_compute_posture_for_tests()


def test_disabled_at_posture_0(monkeypatch):
    from tools.shell import shell_tools_enabled
    assert shell_tools_enabled() is False


def test_enabled_at_posture_1(monkeypatch):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "1")
    c._refreeze_compute_posture_for_tests()
    from tools.shell import shell_tools_enabled
    assert shell_tools_enabled() is True


def test_explicit_off_wins_at_posture_1(monkeypatch):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "1")
    monkeypatch.setenv("SHELL_TOOLS_ENABLED", "false")
    c._refreeze_compute_posture_for_tests()
    from tools.shell import shell_tools_enabled
    assert shell_tools_enabled() is False


def test_shell_and_process_are_delegation_blocked():
    from tools.controller.delegation import DELEGATE_BLOCKED_TOOLS
    assert "shell" in DELEGATE_BLOCKED_TOOLS
    assert "process" in DELEGATE_BLOCKED_TOOLS


def test_shell_not_child_inheritable_by_default():
    from agents.task.goals.dispatcher import CHILD_INHERITABLE_TOOLS
    assert "shell" not in CHILD_INHERITABLE_TOOLS
    assert "process" not in CHILD_INHERITABLE_TOOLS


def test_register_forced_makes_action_registry_introspectable(monkeypatch):
    """Force-register and confirm the shell tool class + its Pydantic param model
    are usable (the `from __future__` param-model landmine would break this)."""
    from tools.shell import register_shell_tools
    from tools.descriptors import get_tool_class
    ok = register_shell_tools(force=True)
    assert ok is True
    cls = get_tool_class("shell")
    assert cls is not None
    # the param model resolves to a real BaseModel subclass (not a stringized annotation)
    from tools.shell.tool import ShellRunParams
    from pydantic import BaseModel
    assert issubclass(ShellRunParams, BaseModel)
    assert "command" in ShellRunParams.model_fields
