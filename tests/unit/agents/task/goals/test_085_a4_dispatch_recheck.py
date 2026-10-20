"""085 W8 A4: the declared tool set is re-judged at dispatch, keyed on the
row's stored actor — a planted or legacy row cannot carry a gated id its
writer could not grant, and a rig the creation check never judged is judged."""
import types

import pytest

from core.tool_grants import dispatch_recheck


def test_an_agent_row_loses_a_gated_id_outside_its_ceiling():
    kept, refused = dispatch_recheck(["filesystem", "defi_trade"], {"actor": "agent"},
                                     declared=["defi_trade"], agent_ceiling={"filesystem"})
    assert kept == ["filesystem"] and refused == ["defi_trade"]


def test_an_owner_seat_row_keeps_everything():
    kept, refused = dispatch_recheck(["defi_trade"], {"actor": "owner_seat"},
                                     declared=["defi_trade"], agent_ceiling=set())
    assert kept == ["defi_trade"] and refused == []


def test_a_legacy_row_without_an_actor_is_unchanged():
    assert dispatch_recheck(["defi_trade"], {}, declared=["defi_trade"])[0] == ["defi_trade"]


def test_an_unknown_actor_holds_no_gated_id():
    kept, refused = dispatch_recheck(["defi_trade", "filesystem"], {"actor": "root"},
                                     declared=["defi_trade", "filesystem"])
    assert refused == ["defi_trade"] and kept == ["filesystem"]


def test_the_default_toolset_is_not_judged():
    # No declared set (no tools, no rig): the deploy's own default stands.
    assert dispatch_recheck(["code_execution"], {"actor": "agent"}, declared=[])[0] == \
        ["code_execution"]


@pytest.fixture
def _unarmed(monkeypatch):
    monkeypatch.setenv("DEFI_AGENT_AUTONOMY", "false")
    monkeypatch.setattr("core.config_policy.full_autonomy_enabled", lambda: False)


def test_the_goal_dispatcher_drops_a_money_rig_an_agent_wrote(_unarmed):
    from agents.task.goals.dispatcher import GoalDispatcher
    d = GoalDispatcher.__new__(GoalDispatcher)
    agent_goal = types.SimpleNamespace(id="g1", parent_id=None, title="t", body="",
                                       payload={"rig": "money_rail", "actor": "agent"})
    assert "defi_trade" not in d._resolve_tools(agent_goal)
    owner_goal = types.SimpleNamespace(id="g2", parent_id=None, title="t", body="",
                                       payload={"rig": "money_rail", "actor": "owner_seat"})
    assert "defi_trade" in d._resolve_tools(owner_goal)
    planted = types.SimpleNamespace(id="g3", parent_id=None, title="t", body="",
                                    payload={"tools": ["defi_trade", "filesystem"],
                                             "actor": "agent"})
    assert d._resolve_tools(planted) == ["filesystem"]


def test_the_cron_read_drops_a_money_rig_an_agent_wrote(_unarmed):
    from tools.cronjob_tools import resolve_cron_tools
    assert "defi_trade" not in resolve_cron_tools({"rig": "money_rail", "actor": "agent"})
    assert "defi_trade" in resolve_cron_tools({"rig": "money_rail", "actor": "owner_seat"})
    assert "defi_trade" in resolve_cron_tools({"rig": "money_rail"})      # legacy row
