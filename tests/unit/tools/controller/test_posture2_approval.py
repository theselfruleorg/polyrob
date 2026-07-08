"""WS-6: posture-2 auto-gates the self-maintenance surface behind approval.

At AGENT_COMPUTE_POSTURE >= 2 the gated-action set UNIONs the recommended
coding/self-evolution set with the compute verbs (shell_run + self_env_*), and the
provider defaults to the interactive one (fail-closed to deny if it can't prompt).
Posture < 2 is byte-identical to the operator's explicit APPROVAL_REQUIRED_TOOLS.
"""
import asyncio

import pytest

import agents.task.constants as c
import tools.controller.approval as approval


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("AGENT_COMPUTE_POSTURE", "APPROVAL_REQUIRED_TOOLS", "APPROVAL_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    c._refreeze_compute_posture_for_tests()
    approval._refreeze_approval_flags_for_tests()
    yield
    c._refreeze_compute_posture_for_tests()
    approval._refreeze_approval_flags_for_tests()


def _posture(monkeypatch, v):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", v)
    c._refreeze_compute_posture_for_tests()


# --- gated-action resolution -----------------------------------------------------

def test_posture_below_2_uses_only_explicit_set(monkeypatch):
    monkeypatch.setenv("APPROVAL_REQUIRED_TOOLS", "git_push")
    approval._refreeze_approval_flags_for_tests()
    _posture(monkeypatch, "1")
    actions, provider = approval.resolve_gated_actions()
    assert actions == {"git_push"}
    assert provider == "auto"  # posture 1 does not force the interactive provider


def test_posture_2_unions_compute_verbs(monkeypatch):
    _posture(monkeypatch, "2")
    actions, provider = approval.resolve_gated_actions()
    assert "shell_run" in actions
    assert "self_env_install_dep" in actions
    assert "self_env_patch_source" in actions
    # and the recommended coding/self-evolution defaults
    assert "git_push" in actions and "mcp_install" in actions
    # provider defaults to interactive (fail-closed to deny if it can't prompt)
    assert provider == "interactive_cli"


def test_posture_2_explicit_provider_wins(monkeypatch):
    monkeypatch.setenv("APPROVAL_PROVIDER", "deny")
    approval._refreeze_approval_flags_for_tests()
    _posture(monkeypatch, "2")
    _actions, provider = approval.resolve_gated_actions()
    assert provider == "deny"  # an explicit provider is not overridden


def test_posture_2_unions_with_operator_set(monkeypatch):
    monkeypatch.setenv("APPROVAL_REQUIRED_TOOLS", "custom_action")
    approval._refreeze_approval_flags_for_tests()
    _posture(monkeypatch, "2")
    actions, _ = approval.resolve_gated_actions()
    assert "custom_action" in actions and "shell_run" in actions


# --- interactive provider behavior ------------------------------------------------

@pytest.mark.asyncio
async def test_interactive_provider_approve():
    from tools.controller.approval_interactive import InteractiveCLIApprover
    prov = InteractiveCLIApprover(input_fn=lambda prompt: "y")
    assert await prov.request("shell_run", {"command": "ls"}, None) is True


@pytest.mark.asyncio
async def test_interactive_provider_deny():
    from tools.controller.approval_interactive import InteractiveCLIApprover
    prov = InteractiveCLIApprover(input_fn=lambda prompt: "n")
    assert await prov.request("shell_run", {"command": "rm -rf /"}, None) is False


@pytest.mark.asyncio
async def test_approval_hook_timeout_denies():
    from tools.controller.approval import make_approval_hook, ApprovalProvider

    class _Hang(ApprovalProvider):
        async def request(self, action_name, params, context):
            await asyncio.sleep(10)
            return True

    hook = make_approval_hook(_Hang(), {"shell_run"}, timeout=0.05)
    reason = await hook("shell_run", {}, None)
    assert reason is not None and "denied" in reason.lower()


@pytest.mark.asyncio
async def test_headless_provider_denies_on_eof():
    """Headless (no TTY): input() raises EOFError -> deny (fail-closed)."""
    from tools.controller.approval_interactive import InteractiveCLIApprover

    def _eof(prompt):
        raise EOFError("no tty")

    prov = InteractiveCLIApprover(input_fn=_eof)
    assert await prov.request("self_env_install_dep", {}, None) is False
