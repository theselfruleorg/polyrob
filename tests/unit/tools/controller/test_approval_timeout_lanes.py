"""030 WS-E2 (finding L10): a durable owner_queue approval wait must get the
transport-realistic timeout (``payment_approval_timeout_sec()``, default 300s),
not the generic in-process 30s default — a phone owner cannot win a 30-second
race. The env var stays ``APPROVAL_TIMEOUT_SEC`` for both; only the *default*
differs by provider. The timeout-deny message must also teach the one-shot
grant so a timed-out owner knows their late approval still applies.
"""
import types

import pytest

import agents.task.constants as constants
import tools.controller.approval as approval


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("AGENT_COMPUTE_POSTURE", "APPROVAL_REQUIRED_TOOLS", "APPROVAL_PROVIDER",
              "APPROVAL_TIMEOUT_SEC"):
        monkeypatch.delenv(k, raising=False)
    constants._refreeze_compute_posture_for_tests()
    approval._refreeze_approval_flags_for_tests()
    yield
    constants._refreeze_compute_posture_for_tests()
    approval._refreeze_approval_flags_for_tests()


def test_owner_queue_wait_defaults_to_payment_class_timeout():
    from core.config_policy import payment_approval_timeout_sec

    assert approval.approval_wait_timeout_sec("owner_queue") == pytest.approx(
        payment_approval_timeout_sec()
    )
    assert approval.approval_wait_timeout_sec("owner_queue") >= 300.0


def test_non_queue_providers_keep_the_generic_default():
    assert approval.approval_wait_timeout_sec("auto") == pytest.approx(
        approval.DEFAULT_APPROVAL_TIMEOUT_SEC
    )
    assert approval.approval_wait_timeout_sec(None) == pytest.approx(
        approval.DEFAULT_APPROVAL_TIMEOUT_SEC
    )


def test_explicit_env_value_wins_for_both(monkeypatch):
    monkeypatch.setenv("APPROVAL_TIMEOUT_SEC", "77")
    approval._refreeze_approval_flags_for_tests()
    from core.config_policy.policy import _refreeze_payment_approval_flags
    _refreeze_payment_approval_flags()
    try:
        assert approval.approval_wait_timeout_sec("owner_queue") == pytest.approx(77.0)
        assert approval.approval_wait_timeout_sec("auto") == pytest.approx(77.0)
    finally:
        monkeypatch.delenv("APPROVAL_TIMEOUT_SEC", raising=False)
        approval._refreeze_approval_flags_for_tests()
        _refreeze_payment_approval_flags()


def _make_controller(tmp_path, user_id="u1"):
    import agents.task.agent.service  # noqa: F401 — avoid controller<->orchestrator import cycle
    from tools.controller.service import Controller

    orch = types.SimpleNamespace(session_id="s1", user_id=user_id, workspace_dir=str(tmp_path))
    container = types.SimpleNamespace(config=types.SimpleNamespace(data_dir=str(tmp_path)))
    return Controller(container=container, orchestrator=orch)


def test_supervised_owner_queue_lane_passes_long_timeout(tmp_path, monkeypatch):
    """Controller.__init__ with APPROVAL_PROVIDER=owner_queue must wire the hook
    with the owner-queue timeout, not the 30s default."""
    monkeypatch.setenv("APPROVAL_REQUIRED_TOOLS", "self_env_install_dep")
    monkeypatch.setenv("APPROVAL_PROVIDER", "owner_queue")
    approval._refreeze_approval_flags_for_tests()

    captured = []
    real = approval.make_approval_hook

    def _spy(provider, required_tools, **kwargs):
        captured.append(kwargs.get("timeout"))
        return real(provider, required_tools, **kwargs)

    monkeypatch.setattr(approval, "make_approval_hook", _spy)
    _make_controller(tmp_path)

    assert captured, "approval hook was never wired"
    assert captured[0] == pytest.approx(approval.approval_wait_timeout_sec("owner_queue"))
    assert captured[0] >= 300.0


@pytest.mark.asyncio
async def test_timeout_deny_message_teaches_the_one_shot_grant():
    class _Never(approval.ApprovalProvider):
        async def request(self, action_name, params, context) -> bool:
            import asyncio
            await asyncio.sleep(60)
            return True

    hook = approval.make_approval_hook(_Never(), {"self_env_install_dep"}, timeout=0.01)
    reason = await hook("self_env_install_dep", {}, None)
    assert reason is not None
    assert "approve now" in reason or "still applies" in reason or "next identical" in reason
