"""owner-UX P1 T5: Controller.__init__ unions pref approvals with the frozen env
gate at construction time. Complements test_approval_prefs.py (which covers only
the pure ``pref_gated_actions`` helper) by asserting the actual WIRING inside
``Controller.__init__`` — the pref-added action is gated end-to-end, the frozen
env accessor is untouched, and ``approvals.deny`` unions into the denylist hook.
"""
import types

import pytest

import agents.task.constants as constants
import tools.controller.approval as approval
from core.prefs import write_preference


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("AGENT_COMPUTE_POSTURE", "APPROVAL_REQUIRED_TOOLS", "APPROVAL_PROVIDER",
              "POLYROB_TOOL_DENYLIST"):
        monkeypatch.delenv(k, raising=False)
    constants._refreeze_compute_posture_for_tests()
    approval._refreeze_approval_flags_for_tests()
    yield
    constants._refreeze_compute_posture_for_tests()
    approval._refreeze_approval_flags_for_tests()


def _make_controller(tmp_path, user_id="u1"):
    import agents.task.agent.service  # noqa: F401 — avoid controller<->orchestrator import cycle
    from tools.controller.service import Controller

    orch = types.SimpleNamespace(session_id="s1", user_id=user_id, workspace_dir=str(tmp_path))
    container = types.SimpleNamespace(config=types.SimpleNamespace(data_dir=str(tmp_path)))
    return Controller(container=container, orchestrator=orch)


@pytest.mark.asyncio
async def test_pref_gates_a_new_action_not_in_env(tmp_path):
    """No APPROVAL_REQUIRED_TOOLS set; a pref ALONE gates + (with a stricter
    provider pref) denies an action the operator never configured."""
    write_preference(tmp_path, "u1", "approvals.require", ["custom_action"])
    write_preference(tmp_path, "u1", "approvals.provider", "deny")
    frozen_before = approval.frozen_approval_required_tools()

    c = _make_controller(tmp_path)

    reason = await c._run_pre_tool_call_hooks("custom_action", {}, None)
    assert reason is not None and "denied" in reason.lower()
    # An action NOT in the (env union pref) gated set is unaffected.
    reason2 = await c._run_pre_tool_call_hooks("read_file", {}, None)
    assert reason2 is None
    # The import-frozen env snapshot is never touched by pref content.
    assert approval.frozen_approval_required_tools() == frozen_before


@pytest.mark.asyncio
async def test_no_pref_file_is_byte_identical(tmp_path):
    """No prefs.toml at all -> legacy behavior: no gating, nothing denied."""
    c = _make_controller(tmp_path)
    reason = await c._run_pre_tool_call_hooks("anything", {}, None)
    assert reason is None


@pytest.mark.asyncio
async def test_denylist_union_with_pref(tmp_path, monkeypatch):
    """approvals.deny pref UNIONs into the POLYROB_TOOL_DENYLIST hook — both the
    operator's env-configured name and the pref-added name end up denied."""
    monkeypatch.setenv("POLYROB_TOOL_DENYLIST", "git_push")
    write_preference(tmp_path, "u1", "approvals.deny", ["dangerous_action"])

    c = _make_controller(tmp_path)

    reason_env = await c._run_pre_tool_call_hooks("git_push", {}, None)
    reason_pref = await c._run_pre_tool_call_hooks("dangerous_action", {}, None)
    assert reason_env is not None
    assert reason_pref is not None


@pytest.mark.asyncio
async def test_provider_pref_can_only_get_stricter(tmp_path, monkeypatch):
    """An explicit operator APPROVAL_PROVIDER=auto stays allow-all unless a pref
    makes it STRICTER; a pref can never loosen a stricter operator setting."""
    monkeypatch.setenv("APPROVAL_REQUIRED_TOOLS", "shell_run")
    monkeypatch.setenv("APPROVAL_PROVIDER", "deny")
    approval._refreeze_approval_flags_for_tests()
    # A pref trying to LOOSEN the provider to "auto" must not win (stricter-of).
    write_preference(tmp_path, "u1", "approvals.provider", "auto")

    c = _make_controller(tmp_path)

    reason = await c._run_pre_tool_call_hooks("shell_run", {}, None)
    assert reason is not None  # still denied — the operator's "deny" wins (stricter)
