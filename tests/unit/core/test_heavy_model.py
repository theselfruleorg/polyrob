"""HEAVY_MODEL: money-class jobs and goals run on a stronger model than the cheap
default (owner decision 2026-10-14: GLM-5.3-Flash default, GLM-5.3 for money work)."""
import pytest

from core.runtime_config import heavy_model_for

PIN = {"DEFAULT_PROVIDER": "openrouter", "DEFAULT_MODEL": "z-ai/glm-5.3-flash",
       "HEAVY_MODEL": "z-ai/glm-5.3"}


def test_money_class_gets_the_heavy_model():
    assert heavy_model_for({"priority": "money"}, "openrouter", env=PIN) == "z-ai/glm-5.3"
    assert heavy_model_for('{"priority": "MONEY"}', "openrouter", env=PIN) == "z-ai/glm-5.3"


@pytest.mark.parametrize("payload", [{}, {"priority": "ops"}, None, "not json"])
def test_other_work_keeps_the_default(payload):
    assert heavy_model_for(payload, "openrouter", env=PIN) is None


def test_unset_flag_or_another_provider_changes_nothing():
    env = dict(PIN); env.pop("HEAVY_MODEL")
    assert heavy_model_for({"priority": "money"}, "openrouter", env=env) is None
    assert heavy_model_for({"priority": "money"}, "anthropic", env=PIN) is None


@pytest.mark.asyncio
async def test_a_money_job_runs_on_the_heavy_model_and_its_own_pin_wins(monkeypatch, tmp_path):
    from agents.task.runtime.run_outcome import RunOutcome
    from cron import runner as cron_runner
    from cron.jobs import CronJob
    from cron.runner import make_agent_runner

    for k, v in PIN.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CHAT_PROVIDER", raising=False)
    monkeypatch.delenv("CHAT_MODEL", raising=False)
    monkeypatch.setenv("CRON_RUN_LOOP", "true")
    seen = []

    async def _none(**kw):
        return [] if "limit" in kw else None

    monkeypatch.setattr("modules.memory.episodic.finalize_episode", _none)
    monkeypatch.setattr("modules.memory.registry.memory_recall_episodes", _none)
    monkeypatch.setattr(cron_runner, "_cron_ev", lambda *a, **k: None)

    async def fake_run(task_agent, *, user_id, request, **kw):
        seen.append(request.get("model"))
        return RunOutcome(session_id="s1", status="Session completed successfully",
                          done_called=True)
    monkeypatch.setattr(cron_runner, "_run_task_to_outcome", fake_run)

    class _Agent:
        container = None

    def _job(payload):
        return CronJob(id="j1", task="buy back", schedule_spec="30m", user_id="u1",
                       next_run_at=None, one_shot=True, skip_memory=True,
                       max_duration_seconds=180, payload=payload, created_at=None)

    run = make_agent_runner(_Agent(), data_dir=str(tmp_path))
    await run(_job({"priority": "money"}))
    await run(_job({"priority": "ops"}))
    await run(_job({"priority": "money", "model": "x-ai/grok-4.3"}))
    assert seen == ["z-ai/glm-5.3", "z-ai/glm-5.3-flash", "x-ai/grok-4.3"]


# --- 2026-10-14 review B-1 / B-8: one money predicate, one resolver -------------------

@pytest.mark.parametrize("payload", [
    {"rig": "money_rail"},                       # the live EXIT CHECK / TKN REBUY shape
    '{"rig": "money_rail"}',
    {"tools": ["message", "defi_trade"]},
    {"tools": ["mcp:x402_pay"]},
    {"priority": "money", "rig": "social"},
])
def test_a_money_rail_without_a_priority_is_money_class(payload):
    from core.money.classify import is_money_class
    assert is_money_class(payload) is True
    assert heavy_model_for(payload, "openrouter", env=PIN) == "z-ai/glm-5.3"


@pytest.mark.parametrize("payload", [{"rig": "social"}, {"rig": "research"},
                                     {"tools": ["message", "web_search"]}, {}, None, "bad"])
def test_a_non_money_toolset_is_not_money_class(payload, monkeypatch):
    monkeypatch.delenv("AUTONOMOUS_RIG_DEFAULT", raising=False)
    from core.money.classify import is_money_class
    assert is_money_class(payload) is False


def test_model_for_precedence():
    from core.runtime_config import model_for
    # explicit payload.model wins over the heavy tier
    assert model_for({"rig": "money_rail", "model": "x/y"}, "openrouter", env=PIN) == "x/y"
    # an explicit heavy tier routes a non-money payload too
    assert model_for({}, "openrouter", tier="heavy", env=PIN) == "z-ai/glm-5.3"
    # inherit = keep the parent's model
    assert model_for({"rig": "money_rail"}, "openrouter", tier="inherit", env=PIN) is None
    assert model_for({"rig": "social"}, "openrouter", env=PIN) is None


def test_regime_rows_carry_a_model_tier():
    from agents.task.session_class import (CHILD, GOAL, REGIME_POLICY, REGIMES,
                                           regime_model_for)
    from core.runtime_config import MODEL_TIERS
    assert all(REGIME_POLICY[r].model_tier in MODEL_TIERS for r in REGIMES)
    assert REGIME_POLICY[CHILD].model_tier == "inherit"
    assert regime_model_for(GOAL, {"rig": "money_rail"}, "openrouter", env=PIN) == "z-ai/glm-5.3"
    assert regime_model_for(CHILD, {"rig": "money_rail"}, "openrouter", env=PIN) is None


def test_the_cron_and_goal_money_tests_are_the_one_predicate():
    from agents.task.goals.dispatcher import _payload_is_money, _payload_preempts
    from cron.jobs import is_money_job

    class _J:
        payload = {"rig": "money_rail"}
    assert is_money_job(_J()) is True
    assert _payload_is_money('{"rig": "money_rail"}') is True
    assert _payload_preempts({"rig": "money_rail"}) is True
    assert _payload_preempts({"rig": "money_rail", "preempts": False}) is False
