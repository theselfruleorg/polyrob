"""067 P0.11: hf_deploy reaches core/agents only through the gate + reconciler seams."""

import asyncio

from core import boot_reconcilers, tool_gates


def test_hf_deploy_registers_gate_and_reconciler():
    import tools.hf_deploy as hf

    assert boot_reconcilers.boot_reconciler("hf_deploy") is hf._boot_reconcile
    assert tool_gates.gate_for("hf_deploy") is not None


def test_no_registration_means_no_sweep_and_gate_off(monkeypatch):
    import core.autonomy_runtime as rt
    from agents.task.goals import dispatcher

    monkeypatch.setattr(tool_gates, "_GATES", {})
    monkeypatch.setattr(boot_reconcilers, "_RECONCILERS", {})
    monkeypatch.setenv("HF_DEPLOY_ENABLED", "true")
    assert rt._hf_deploy_enabled() is False
    monkeypatch.setattr(dispatcher, "_compute_posture_at_least_2", lambda: True)
    assert dispatcher._hf_deploy_goal_tool_enabled() is False

    async def drive():
        rt._schedule_hf_deploy_reconcile()
        await asyncio.sleep(0.01)

    asyncio.run(drive())  # no gate -> returns before scheduling


def test_registered_gate_but_no_reconciler_is_a_quiet_noop(monkeypatch):
    import core.autonomy_runtime as rt

    monkeypatch.setattr(tool_gates, "_GATES", {"hf_deploy": lambda: True})
    monkeypatch.setattr(boot_reconcilers, "_RECONCILERS", {})

    async def drive():
        rt._schedule_hf_deploy_reconcile()
        await asyncio.sleep(0.01)

    asyncio.run(drive())


def test_dispatcher_gate_follows_the_flag(monkeypatch):
    import tools.hf_deploy  # noqa: F401
    from agents.task.goals import dispatcher

    monkeypatch.setattr(dispatcher, "_compute_posture_at_least_2", lambda: True)
    monkeypatch.delenv("HF_DEPLOY_ENABLED", raising=False)
    assert dispatcher._hf_deploy_goal_tool_enabled() is False
    monkeypatch.setenv("HF_DEPLOY_ENABLED", "true")
    assert dispatcher._hf_deploy_goal_tool_enabled() is True
