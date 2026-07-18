"""The security contract — every deny path gets an explicit test (proposal §7):
posture<2, leaf/sub-agent, forged turn, anonymous tenant, cap exceeded,
min-interval, unapproved app, approved app unattended."""
import asyncio

import pytest

import agents.task.constants as _constants
from tests.unit.tools.hf_deploy.test_tool_deploy import (
    Approver, FakeBroker, make_tool, run_deploy,
)


def _ctx(**kw):
    from tools.controller.execution_context import ActionExecutionContext
    base = dict(session_id="sess-hf", user_id="owner-1", role="orchestrator",
                is_sub_agent=False)
    base.update(kw)
    return ActionExecutionContext(**base)


@pytest.fixture
def posture2(monkeypatch):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "2")
    _constants._refreeze_compute_posture_for_tests()
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-1")
    yield
    import os
    os.environ.pop("AGENT_COMPUTE_POSTURE", None)
    _constants._refreeze_compute_posture_for_tests()


def test_deploy_denied_below_posture_2(tmp_path, monkeypatch, deploy_env, green_orch):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "1")
    _constants._refreeze_compute_posture_for_tests()
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-1")
    tool = make_tool(tmp_path, orch=green_orch)
    res = run_deploy(tool, _ctx())
    assert res.error and "AGENT_COMPUTE_POSTURE" in res.error
    assert tool._broker.deployed == []
    import os
    os.environ.pop("AGENT_COMPUTE_POSTURE", None)
    _constants._refreeze_compute_posture_for_tests()


def test_deploy_denied_for_leaf_role(tmp_path, posture2, deploy_env, green_orch):
    tool = make_tool(tmp_path, orch=green_orch)
    res = run_deploy(tool, _ctx(role="leaf"))
    assert res.error
    assert tool._broker.deployed == []


def test_deploy_denied_for_sub_agent(tmp_path, posture2, deploy_env, green_orch):
    tool = make_tool(tmp_path, orch=green_orch)
    res = run_deploy(tool, _ctx(is_sub_agent=True))
    assert res.error
    assert tool._broker.deployed == []


@pytest.mark.parametrize("kind", ["self_wake", "delegation_result"])
def test_deploy_denied_on_forged_turn(tmp_path, posture2, deploy_env, green_orch, kind):
    tool = make_tool(tmp_path, orch=green_orch)
    res = run_deploy(tool, _ctx(metadata={"turn_kind": kind}))
    assert res.error
    assert tool._broker.deployed == []


def test_deploy_denied_for_non_owner_tenant(tmp_path, posture2, deploy_env, green_orch):
    tool = make_tool(tmp_path, orch=green_orch)
    res = run_deploy(tool, _ctx(user_id="u_stranger"))
    assert res.error
    assert tool._broker.deployed == []


def test_deploy_denied_without_execution_context(tmp_path, posture2, deploy_env, green_orch):
    tool = make_tool(tmp_path, orch=green_orch)
    res = run_deploy(tool, None)
    assert res.error
    assert tool._broker.deployed == []


def test_first_publish_of_unknown_app_requires_approval(
        tmp_path, posture2, deploy_env, green_orch):
    approver = Approver(allow=False)
    tool = make_tool(tmp_path, approver=approver, orch=green_orch)
    res = run_deploy(tool, _ctx())
    assert res.error and "approval" in res.error.lower()
    assert approver.requests, "the approval provider MUST be asked for a first publish"
    assert tool._broker.deployed == []
    # the app is registered pending so the owner can approve it later
    row = tool._registry.get("app-x", "owner-1")
    assert row and row["status"] == "pending" and row["approved_at"] is None


def test_first_publish_approved_proceeds_and_marks_approved(
        tmp_path, posture2, deploy_env, green_orch):
    approver = Approver(allow=True)
    tool = make_tool(tmp_path, approver=approver, orch=green_orch)
    res = run_deploy(tool, _ctx())
    assert not res.error
    assert approver.requests
    assert tool._registry.get("app-x", "owner-1")["approved_at"] is not None


def test_redeploy_of_approved_app_is_unattended(
        tmp_path, posture2, deploy_env, green_orch, monkeypatch):
    monkeypatch.setenv("HF_DEPLOY_MIN_INTERVAL_SEC", "0")
    approver = Approver(allow=True)
    tool = make_tool(tmp_path, approver=approver, orch=green_orch)
    assert not run_deploy(tool, _ctx()).error
    assert len(approver.requests) == 1
    # second deploy: no new approval request
    assert not run_deploy(tool, _ctx()).error
    assert len(approver.requests) == 1


def test_first_publish_uses_real_provider_not_autoapprover(
        tmp_path, posture2, deploy_env, green_orch, monkeypatch):
    # posture>=2, APPROVAL_PROVIDER unset, NO injected provider: production
    # resolution must NOT rubber-stamp (AutoApprover=allow-all). It resolves the
    # SAME interactive-default provider the Controller uses at posture>=2, which
    # fail-closes to deny when it can't prompt (headless) — so a brand-new PUBLIC
    # app can never be first-published unattended.
    monkeypatch.delenv("APPROVAL_PROVIDER", raising=False)
    from tools.controller.approval import AutoApprover

    tool = make_tool(tmp_path, orch=green_orch)
    tool._approval_provider = None  # drop the injected test approver
    provider = tool._get_approval_provider()
    assert not isinstance(provider, AutoApprover), (
        "first-publish must resolve a REAL provider (interactive default / deny), "
        "never AutoApprover=rubber-stamp")

    # And a first publish under it does not silently proceed. Force the (no-TTY)
    # interactive read to fail deterministically so the test never blocks on stdin.
    monkeypatch.setattr("builtins.input",
                        lambda *a, **k: (_ for _ in ()).throw(EOFError("no tty")))
    tool._approval_provider = None  # re-resolve fresh so it binds the patched input
    res = run_deploy(tool, _ctx())
    assert res.error
    assert tool._broker.deployed == []
    row = tool._registry.get("app-x", "owner-1")
    assert row and row["status"] == "pending" and row["approved_at"] is None


def _reset_approval_freeze(monkeypatch):
    """These tests assert on resolve_gated_actions()'s EXACT resolved provider
    name, which reads a module-global frozen snapshot (WS-7) — not just live
    env. Another suite that ran earlier in the same process (e.g.
    tests/unit/tools/controller/test_autonomy_mode_approvals.py) may have set
    ``APPROVAL_PROVIDER``/``APPROVAL_REQUIRED_TOOLS`` and refrozen; re-snapshot
    from a clean env here so this test's outcome doesn't depend on suite run
    order (belt-and-suspenders — this file otherwise never touches those
    globals)."""
    import tools.controller.approval as approval
    monkeypatch.delenv("APPROVAL_PROVIDER", raising=False)
    monkeypatch.delenv("APPROVAL_REQUIRED_TOOLS", raising=False)
    approval._refreeze_approval_flags_for_tests()
    return approval


def test_supervised_mode_resolution_stays_interactive_cli(
        tmp_path, posture2, deploy_env, green_orch, monkeypatch):
    """Control for the Finding-1 regression below: with AUTONOMY_MODE unset
    (supervised — the posture2 fixture doesn't touch it), resolve_gated_actions()
    keeps defaulting to interactive_cli at posture>=2 — the auto_notify mapping
    only engages under effective AUTONOMY_MODE=autonomous."""
    _reset_approval_freeze(monkeypatch)
    from tools.controller.approval import resolve_gated_actions

    _required, provider_name = resolve_gated_actions()
    assert provider_name == "interactive_cli"


def test_first_publish_maps_auto_notify_to_owner_queue_not_autoapprover(
        tmp_path, posture2, deploy_env, green_orch, monkeypatch):
    """Fix pass (013 T4 review, Finding 1): under effective AUTONOMY_MODE=autonomous
    the GENERIC approval seam (tools/controller/approval.py::resolve_gated_actions)
    now defaults to `auto_notify` (allow + audit + post-hoc notify — see
    tests/unit/tools/controller/test_autonomy_mode_approvals.py). hf_deploy's own
    first-publish gate (tool.py:_get_approval_provider) independently calls
    resolve_gated_actions() for its OWN approval decision (outside the Controller
    hook pipeline) — it must NEVER honor `auto_notify` directly (that would
    silently first-publish a brand-new PUBLIC Space from an unattended run,
    inverting the documented invariant at tool.py:13/:149). It must resolve
    `owner_queue` instead (durable + remotely approvable), never AutoNotifyApprover."""
    _reset_approval_freeze(monkeypatch)
    monkeypatch.setenv("AUTONOMY_MODE", "autonomous")
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    _constants.reset_autonomy_mode_warnings()
    assert _constants.full_autonomy_enabled() is True  # precondition

    from tools.controller.approval import AutoNotifyApprover, resolve_gated_actions
    _required, provider_name = resolve_gated_actions()
    assert provider_name == "auto_notify"  # precondition: the generic seam picked it

    tool = make_tool(tmp_path, orch=green_orch)
    tool._approval_provider = None  # drop the injected test approver, force real resolution
    provider = tool._get_approval_provider()
    assert not isinstance(provider, AutoNotifyApprover), (
        "hf_deploy must never honor the generic allow-all auto_notify lane for a "
        "first PUBLIC publish")


def test_first_publish_denied_for_autonomous_goal_run_under_full_autonomy(
        tmp_path, posture2, deploy_env, green_orch, monkeypatch):
    """An unattended goal/cron-spawned session (marked autonomous — the same
    in-process marker `is_autonomous()` checks) must still be DENIED a first
    publish even under AUTONOMY_MODE=autonomous: owner_queue's own
    forged/autonomous-turn check (tools/controller/approval_queue.py::
    OwnerQueueApprover.request) denies WITHOUT creating a durable ask or
    polling — mirroring interactive_cli's headless fail-closed behavior, so this
    never blocks/hangs."""
    monkeypatch.setenv("AUTONOMY_MODE", "autonomous")
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    _constants.reset_autonomy_mode_warnings()

    from agents.task.goals.autonomy_marker import mark_autonomous
    mark_autonomous("sess-hf")  # the _ctx() default session_id

    tool = make_tool(tmp_path, orch=green_orch)
    tool._approval_provider = None
    res = run_deploy(tool, _ctx())
    assert res.error and "approval" in res.error.lower()
    assert tool._broker.deployed == []
    row = tool._registry.get("app-x", "owner-1")
    assert row and row["status"] == "pending" and row["approved_at"] is None


def test_first_publish_still_queues_for_a_genuine_owner_turn_under_full_autonomy(
        tmp_path, posture2, deploy_env, green_orch, monkeypatch):
    """Not a blanket freeze: a genuine (non-autonomous, non-forged) owner turn
    under AUTONOMY_MODE=autonomous still reaches owner_queue's durable-ask path
    (proven by a real owner decision unblocking it) rather than being denied
    outright or rubber-stamped."""
    _reset_approval_freeze(monkeypatch)
    monkeypatch.setenv("AUTONOMY_MODE", "autonomous")
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    _constants.reset_autonomy_mode_warnings()

    tool = make_tool(tmp_path, orch=green_orch)
    tool._approval_provider = None
    tool._get_approval_provider()  # force real resolution -> OwnerQueueApprover
    from tools.controller.approval_queue import OwnerQueueApprover
    assert isinstance(tool._approval_provider, OwnerQueueApprover)
    tool._approval_provider._home_dir = str(tmp_path)  # keep the asks store in tmp_path
    tool._approval_provider._poll_interval = 0.01

    # run_deploy() wraps asyncio.run(...) — drive the coroutine directly here
    # instead so we can approve mid-flight.
    from tools.hf_deploy.tool import DeployParams

    async def _drive():
        deploy_task = asyncio.ensure_future(
            tool.deploy(DeployParams(app_name="app-x"), execution_context=_ctx()))
        board = tool._approval_provider._board()
        for _ in range(200):
            await asyncio.sleep(0.01)
            from agents.task.goals.board import ASK_OPEN
            asks = board.asks(user_id="owner-1", status=ASK_OPEN)
            if asks:
                board.decide_ask(asks[0].id, user_id="owner-1", approved=True)
                break
        else:
            raise AssertionError("owner_queue never created a durable ask")
        return await deploy_task

    res = asyncio.run(_drive())
    assert not res.error, res.error
    row = tool._registry.get("app-x", "owner-1")
    assert row and row["approved_at"] is not None


def test_approval_provider_error_fails_closed(
        tmp_path, posture2, deploy_env, green_orch):
    class BoomApprover:
        async def request(self, action_name, params, context):
            raise RuntimeError("provider down")

    tool = make_tool(tmp_path, approver=BoomApprover(), orch=green_orch)
    res = run_deploy(tool, _ctx())
    assert res.error
    assert tool._broker.deployed == []


def test_daily_cap_enforced(tmp_path, posture2, deploy_env, green_orch, monkeypatch):
    monkeypatch.setenv("HF_DEPLOY_DAILY_MAX", "2")
    monkeypatch.setenv("HF_DEPLOY_MIN_INTERVAL_SEC", "0")
    tool = make_tool(tmp_path, orch=green_orch)
    assert not run_deploy(tool, _ctx()).error
    assert not run_deploy(tool, _ctx()).error
    res = run_deploy(tool, _ctx())
    assert res.error and "HF_DEPLOY_DAILY_MAX" in res.error
    assert len(tool._broker.deployed) == 2


def test_min_interval_enforced_per_app(tmp_path, posture2, deploy_env, green_orch, monkeypatch):
    monkeypatch.setenv("HF_DEPLOY_MIN_INTERVAL_SEC", "3600")
    tool = make_tool(tmp_path, orch=green_orch)
    assert not run_deploy(tool, _ctx()).error
    res = run_deploy(tool, _ctx())
    assert res.error and "HF_DEPLOY_MIN_INTERVAL_SEC" in res.error
    # a DIFFERENT app is not throttled by app-x's attempt
    res2 = run_deploy(tool, _ctx(), app_name="app-z")
    assert not res2.error


def test_deploy_denied_for_anonymous_tenant(tmp_path, posture2, deploy_env, green_orch,
                                            monkeypatch):
    # the local-operator bypass would own "local"; an EMPTY uid must always refuse
    tool = make_tool(tmp_path, orch=green_orch)
    res = run_deploy(tool, _ctx(user_id=""))
    assert res.error
    assert tool._broker.deployed == []


def test_undeploy_denied_below_posture_2(tmp_path, monkeypatch, deploy_env):
    from tools.hf_deploy.tool import UndeployParams
    monkeypatch.delenv("AGENT_COMPUTE_POSTURE", raising=False)
    _constants._refreeze_compute_posture_for_tests()
    tool = make_tool(tmp_path)
    res = asyncio.run(tool.undeploy(UndeployParams(app_name="app-x"),
                                    execution_context=_ctx()))
    assert res.error
    assert tool._broker.deleted == []
