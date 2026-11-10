"""112 W2: a tier on a goal/job — owner turn only, a reason required, checked at write time."""
import inspect

import pytest

import agents.task.constants  # noqa: F401 — registers the light resolver
from tools.model_tier import tier_payload


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DEFAULT_PROVIDER", "openrouter")
    monkeypatch.setenv("DEFAULT_MODEL", "deepseek/deepseek-v4.1-flash")
    monkeypatch.setenv("HEAVY_MODEL", "deepseek/deepseek-v4-pro")


def test_no_tier_writes_nothing():
    assert tier_payload(None, None, owner_turn=False) == ({}, None)


def test_off_an_owner_turn_is_refused():
    keys, err = tier_payload("heavy", "decides a payout", owner_turn=False)
    assert keys == {} and "genuine owner turn" in err


def test_owner_turn_stores_tier_and_reason():
    keys, err = tier_payload("heavy", "decides  a\npayout", owner_turn=True, provider="openrouter")
    assert err is None and keys == {"tier": "heavy", "tier_why": "decides a payout"}


def test_reason_required_and_unavailable_tier_refused_at_write(monkeypatch):
    assert "requires 'why'" in tier_payload("heavy", " ", owner_turn=True)[1]
    monkeypatch.delenv("HEAVY_MODEL")
    assert "not configured" in tier_payload("heavy", "w", owner_turn=True, provider="openrouter")[1]


def test_goal_and_cron_actions_carry_tier_and_check_it():
    from tools.cronjob_tools import CronEditAction, CronScheduleAction
    from tools.goal_tools import GoalCreateAction, GoalUpdateAction
    for model in (GoalCreateAction, GoalUpdateAction, CronScheduleAction, CronEditAction):
        assert {"tier", "why"} <= set(model.model_fields), model.__name__
    import tools.cronjob_tools as cj
    import tools.goal_tools as gt
    for mod in (gt, cj):
        assert inspect.getsource(mod).count("tier_payload(params.tier, params.why, owner_turn") == 2


def test_model_for_reads_the_stored_tier():
    from core.runtime_config import model_for
    assert model_for({"tier": "heavy", "tier_why": "w"}, "openrouter") == "deepseek/deepseek-v4-pro"
