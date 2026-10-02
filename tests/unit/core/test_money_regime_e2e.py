"""The money regime, end to end (handoff 2026-09-23: "money rails from goals").

The owner's bar: when the dangerous regime is ARMED by flags, a goal the agent
wrote itself — and a cron job — must reach a signature bounded by CAPS, not by
toolset refusals. When it is NOT armed, the 2026-09-23 security fixes hold.

Each test walks the real seams in order — goal_create / cronjob_schedule ->
dispatch tool resolution -> tx_guard.authorize (real turn-origin detectors, a
mocked simulation) -> the spend lane — with the flags set the way prod sets them.
"""
import asyncio
import types

import pytest

from agents.task.goals import autonomy_marker
from agents.task.goals.board import GoalBoard
from core.config_policy.money_regime import (
    REGIME_ARMED, REGIME_AUTONOMOUS, REGIME_SUPERVISED, money_regime,
    money_regime_display)
from tools.goal_tools import GoalCreateAction, GoalTool

OWNER = "owner-1"
_ARM = {
    "DEFI_AGENT_AUTONOMY": "true",
    "DEFI_TRADE_ENABLED": "true",
    "DEFI_AUTONOMOUS_TURN_TRADING": "true",
    "DEFI_TIERED_SPEND_LANE": "true",
}
_ALL = tuple(_ARM) + ("AUTONOMY_MODE", "WALLET_DAILY_CAP_USD", "DEFI_AUTONOMOUS_MAX_USD")


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for name in _ALL:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", OWNER)
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "home"))
    from core.config_policy import reset_autonomy_mode_warnings
    reset_autonomy_mode_warnings()


def _autonomous(monkeypatch):
    monkeypatch.setenv("AUTONOMY_MODE", "autonomous")


def _armed(monkeypatch):
    _autonomous(monkeypatch)
    for k, v in _ARM.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def goal_turn():
    """A goal-dispatched MAIN-agent turn on the owner tenant (what the
    dispatcher builds: orchestrator role, the session marked autonomous)."""
    sid = "sess-goal-regime"
    autonomy_marker.mark_autonomous(sid, "goal-x")
    try:
        yield types.SimpleNamespace(user_id=OWNER, role="orchestrator",
                                    is_sub_agent=False, session_id=sid, metadata={})
    finally:
        autonomy_marker._SESSIONS.pop(sid, None)


def _owner_chat_turn():
    return types.SimpleNamespace(user_id=OWNER, role="orchestrator", is_sub_agent=False,
                                 session_id="sess-owner-chat", metadata={})


def _leaf_turn():
    return types.SimpleNamespace(user_id=OWNER, role="leaf", is_sub_agent=True,
                                 session_id="sess-leaf", metadata={})


def _goal_tool(tmp_path):
    tool = GoalTool.__new__(GoalTool)
    tool._goal_board = GoalBoard(str(tmp_path / "goals.db"))
    return tool


def _create(tool, ctx, **kw):
    kw.setdefault("title", "rebalance the treasury into the target mix")
    return asyncio.run(tool.goal_create(GoalCreateAction(**kw), ctx))


def _dispatch_tools(tool):
    from agents.task.goals.dispatcher import GoalDispatcher
    goal = tool._resolve_board().list_recent(user_id=OWNER, limit=1)[0]
    disp = GoalDispatcher.__new__(GoalDispatcher)
    disp.board = tool._resolve_board()
    return goal, disp._resolve_tools(goal)


# --- the predicate ----------------------------------------------------------

class TestRegime:
    def test_default_is_supervised(self):
        assert money_regime().name == REGIME_SUPERVISED

    def test_autonomous_names_every_missing_key(self, monkeypatch):
        _autonomous(monkeypatch)
        r = money_regime()
        assert r.name == REGIME_AUTONOMOUS
        assert set(r.missing) == set(_ARM)
        assert "DEFI_AUTONOMOUS_TURN_TRADING" in money_regime_display()

    def test_a_partial_arm_is_not_armed(self, monkeypatch):
        _armed(monkeypatch)
        monkeypatch.delenv("DEFI_TIERED_SPEND_LANE")
        assert money_regime() == (REGIME_AUTONOMOUS, ("DEFI_TIERED_SPEND_LANE",))

    def test_a_disabled_daily_cap_is_not_armed(self, monkeypatch):
        _armed(monkeypatch)
        monkeypatch.setenv("WALLET_DAILY_CAP_USD", "none")
        assert money_regime().missing == ("WALLET_DAILY_CAP_USD",)

    def test_all_keys_arm_it(self, monkeypatch):
        _armed(monkeypatch)
        assert money_regime().name == REGIME_ARMED
        assert money_regime_display().startswith("armed")

    def test_the_mode_clamp_wins(self, monkeypatch):
        """A multi-tenant server never arms, whatever the DEFI_* keys say."""
        _armed(monkeypatch)
        monkeypatch.delenv("POLYROB_LOCAL")
        assert money_regime().name == REGIME_SUPERVISED

    def test_the_posture_card_shows_it_in_one_line(self, monkeypatch):
        _armed(monkeypatch)
        from core.config_policy.posture_card import render_posture_card
        lines = [l for l in render_posture_card() if l.startswith("money regime:")]
        assert len(lines) == 1 and "armed" in lines[0]


# --- a self-authored goal -----------------------------------------------------

class TestSelfAuthoredGoal:
    def test_armed_money_rail_goal_dispatches_with_defi_trade(
            self, monkeypatch, tmp_path, goal_turn):
        _armed(monkeypatch)
        tool = _goal_tool(tmp_path)
        res = _create(tool, goal_turn, rig="money_rail")
        assert res.error is None, res.error
        goal, tools = _dispatch_tools(tool)
        assert goal.payload["authored_by"] == "agent"
        assert "defi_trade" in tools

    def test_unarmed_money_rail_goal_is_refused(self, monkeypatch, tmp_path, goal_turn):
        _autonomous(monkeypatch)  # autonomous, money NOT armed
        tool = _goal_tool(tmp_path)
        res = _create(tool, goal_turn, rig="money_rail")
        assert res.error and "defi_trade" in res.error

    def test_supervised_money_rail_goal_is_refused(self, tmp_path, goal_turn):
        tool = _goal_tool(tmp_path)
        res = _create(tool, goal_turn, rig="money_rail")
        assert res.error and "NOT granted" in res.error

    def test_a_stored_agent_row_is_narrowed_when_disarmed(self, monkeypatch, tmp_path,
                                                          goal_turn):
        """Armed at creation, disarmed at dispatch: the row loses defi_trade."""
        _armed(monkeypatch)
        tool = _goal_tool(tmp_path)
        assert _create(tool, goal_turn, rig="money_rail").error is None
        monkeypatch.delenv("DEFI_AGENT_AUTONOMY")
        _goal, tools = _dispatch_tools(tool)
        assert "defi_trade" not in tools


# --- owner-seat provenance ----------------------------------------------------

class TestOwnerChatTurn:
    def test_owner_asking_in_chat_sets_an_owner_goal(self, tmp_path):
        """Supervised regime: the owner's own chat turn is an owner seat."""
        tool = _goal_tool(tmp_path)
        res = _create(tool, _owner_chat_turn(), rig="money_rail")
        assert res.error is None, res.error
        goal, tools = _dispatch_tools(tool)
        assert goal.payload["authored_by"] == "owner"
        assert "defi_trade" in tools

    def test_owner_turn_tools_reach_the_rig_ids_only(self, tmp_path):
        tool = _goal_tool(tmp_path)
        _create(tool, _owner_chat_turn(), tools=["defi_trade", "shell", "x402_pay"])
        goal = tool._resolve_board().list_recent(user_id=OWNER, limit=1)[0]
        assert "defi_trade" in goal.payload["tools"]
        assert "shell" not in goal.payload["tools"]
        assert "x402_pay" not in goal.payload["tools"]

    @pytest.mark.parametrize("ctx", [
        _leaf_turn(),
        types.SimpleNamespace(user_id=OWNER, role="orchestrator", is_sub_agent=False,
                              session_id="s", metadata={"turn_kind": "self_wake"}),
        types.SimpleNamespace(user_id=OWNER, role="orchestrator", is_sub_agent=False,
                              session_id="s", metadata={"turn_kind": "group"}),
        types.SimpleNamespace(user_id="u_stranger", role="orchestrator",
                              is_sub_agent=False, session_id="s", metadata={}),
    ])
    def test_non_owner_turns_stay_agent_authored(self, tmp_path, ctx):
        tool = _goal_tool(tmp_path)
        res = _create(tool, ctx, rig="money_rail")
        # A leaf is refused outright (it cannot create a durable goal at all).
        assert res.error and ("NOT granted" in res.error or "leaf" in res.error)


# --- cron ---------------------------------------------------------------------

def _cron_tool(tmp_path):
    from cron.jobs import CronJobStore
    from cron.service import CronService
    from tools.cronjob_tools import CronJobTool
    tool = CronJobTool.__new__(CronJobTool)
    tool._cron_service = CronService(CronJobStore(str(tmp_path / "cron.db")))
    return tool


def _schedule(tool, ctx, **kw):
    from tools.cronjob_tools import CronScheduleAction
    kw.setdefault("task", "buy back the token within the daily budget")
    kw.setdefault("schedule", "every 6h")
    return asyncio.run(tool.cronjob_schedule(CronScheduleAction(**kw), ctx))


class TestCron:
    def test_armed_goal_run_schedules_a_money_rail_job(self, monkeypatch, tmp_path,
                                                       goal_turn):
        _armed(monkeypatch)
        tool = _cron_tool(tmp_path)
        res = _schedule(tool, goal_turn, rig="money_rail")
        assert res.error is None, res.error
        from cron.runner import resolve_cron_tools
        job = tool._resolve_service().list_jobs(user_id=OWNER)[0]
        assert job.payload["authored_by"] == "agent"
        assert "defi_trade" in resolve_cron_tools(job.payload)

    def test_autonomous_goal_run_may_schedule_but_not_money(self, monkeypatch,
                                                            tmp_path, goal_turn):
        _autonomous(monkeypatch)
        tool = _cron_tool(tmp_path)
        assert _schedule(tool, goal_turn, rig="research").error is None
        res = _schedule(tool, goal_turn, rig="money_rail")
        assert res.error and "defi_trade" in res.error

    def test_supervised_goal_run_cannot_schedule(self, tmp_path, goal_turn):
        res = _schedule(_cron_tool(tmp_path), goal_turn)
        assert res.error and "genuine owner turn" in res.error

    def test_armed_leaf_and_room_turns_still_cannot_schedule(self, monkeypatch, tmp_path,
                                                             goal_turn):
        _armed(monkeypatch)
        tool = _cron_tool(tmp_path)
        assert _schedule(tool, _leaf_turn()).error
        goal_turn.metadata["turn_kind"] = "group"
        assert _schedule(tool, goal_turn).error

    def test_owner_chat_turn_schedules_an_owner_money_job(self, tmp_path):
        tool = _cron_tool(tmp_path)
        res = _schedule(tool, _owner_chat_turn(), rig="money_rail")
        assert res.error is None, res.error
        from cron.runner import resolve_cron_tools
        job = tool._resolve_service().list_jobs(user_id=OWNER)[0]
        assert job.payload["authored_by"] == "owner"
        assert "defi_trade" in resolve_cron_tools(job.payload)


# --- the trade reaches the guard and passes within caps -----------------------

def _authorize(ctx, monkeypatch, *, daily_cap=50.0):
    from core.wallet import tx_guard
    from core.wallet.policy import PolicyGate
    from core.wallet.simulation import Deltas
    from tools.controller.turn_origin import (_is_autonomous_goal_turn,
                                              _is_forged_or_autonomous_turn)
    monkeypatch.setattr(tx_guard, "_decimals_for", lambda _c, _t: 6)
    token = "0x" + "1" * 40
    intent = tx_guard.TxIntent(chain="base", token=token, to="0x" + "2" * 40,
                               amount_raw=1_000_000, max_spend_usd=5.0,
                               idempotency_key="regime-e2e")
    return tx_guard.authorize(
        intent, {"to": "0x" + "2" * 40, "data": "0x"},
        holder="0x" + "3" * 40,
        gate=PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=daily_cap),
        execution_context=ctx, tool_self=None,
        halted_fn=lambda: False, entry_paused_fn=lambda: False,
        forged_fn=_is_forged_or_autonomous_turn,
        autonomous_ok_fn=_is_autonomous_goal_turn,
        rpc_is_pinned_fn=lambda _c: True,
        simulate_fn=lambda **kw: Deltas(ok=True, token_deltas={token: -1_000_000},
                                        gas_used=100_000),
        price_fn=lambda _c, _t: 1.0)


class TestTradeReachesTheGuard:
    def test_armed_goal_turn_trade_passes_within_caps(self, monkeypatch, goal_turn):
        _armed(monkeypatch)
        d = _authorize(goal_turn, monkeypatch)
        assert d.allowed, d.reason
        assert d.lane == "autonomous"

    def test_armed_spend_lane_runs_it_act_and_report(self, monkeypatch):
        _armed(monkeypatch)
        from core.config_policy.spend_lane import spend_exemption
        assert spend_exemption("defi_trade_swap", {"dry_run": False, "max_spend_usd": 5.0})
        # H03a stays tiered, not blanket: within the ceiling AND the session budget.
        assert spend_exemption("dapp_browser_dapp_connect",
                               {"max_spend_usd": 5.0, "session_budget_usd": 10.0})
        assert spend_exemption("dapp_browser_dapp_connect",
                               {"max_spend_usd": 5.0, "session_budget_usd": 10_000.0}) is None

    def test_unarmed_goal_turn_trade_is_refused(self, monkeypatch, goal_turn):
        _autonomous(monkeypatch)
        d = _authorize(goal_turn, monkeypatch)
        assert not d.allowed and "forged/autonomous turn" in d.reason

    def test_unarmed_spend_lane_keeps_the_tap(self, monkeypatch):
        _autonomous(monkeypatch)
        from core.config_policy.spend_lane import spend_exemption
        assert spend_exemption("defi_trade_swap",
                               {"dry_run": False, "max_spend_usd": 5.0}) is None

    def test_armed_leaf_turn_trade_is_refused(self, monkeypatch):
        _armed(monkeypatch)
        d = _authorize(_leaf_turn(), monkeypatch)
        assert not d.allowed and "forged/autonomous turn" in d.reason

    def test_armed_self_wake_trade_is_refused(self, monkeypatch, goal_turn):
        _armed(monkeypatch)
        goal_turn.metadata["turn_kind"] = "self_wake"
        d = _authorize(goal_turn, monkeypatch)
        assert not d.allowed


# --- the deploy-wide default rig is a DEFAULT, not a grant ---------------------

class TestDefaultRig:
    def test_env_money_rail_does_not_arm_agent_work(self, monkeypatch):
        """AUTONOMOUS_RIG_DEFAULT=money_rail used to hand defi_trade to every
        agent-authored row, bypassing DEFI_AGENT_AUTONOMY and the mode."""
        monkeypatch.setenv("AUTONOMOUS_RIG_DEFAULT", "money_rail")
        from cron.runner import resolve_cron_tools
        assert "defi_trade" not in resolve_cron_tools({"authored_by": "agent"})
        assert "defi_trade" in resolve_cron_tools({"authored_by": "owner"})
        assert "defi_trade" in resolve_cron_tools({})  # an owner-seat row

    def test_armed_env_money_rail_reaches_agent_work(self, monkeypatch):
        _armed(monkeypatch)
        monkeypatch.setenv("AUTONOMOUS_RIG_DEFAULT", "money_rail")
        from cron.runner import resolve_cron_tools
        assert "defi_trade" in resolve_cron_tools({"authored_by": "agent"})


# --- every guard call can recognise a goal turn --------------------------------

def test_every_guard_call_passes_the_goal_turn_detector():
    """`wrap`, `unwrap`, `transfer`, `call`, deploy, LP, launchpad and the dapp
    bridge passed `forged_fn` but not `autonomous_ok_fn`, so under
    DEFI_AUTONOMOUS_TURN_TRADING a goal run could swap but never transfer: tx_guard
    fails closed without the detector. A call site that passes one passes both."""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[3]
    offenders = []
    for path in list((root / "tools").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"forged_fn=_is_forged_or_autonomous_turn", text):
            window = text[m.end():m.end() + 200]
            if "autonomous_ok_fn=" not in window.split(")")[0]:
                offenders.append(f"{path.relative_to(root)}:{text[:m.start()].count(chr(10)) + 1}")
    assert not offenders, offenders
