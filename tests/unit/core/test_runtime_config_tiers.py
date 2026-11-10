"""112 W2: the named tiers the agent picks — the operator's env decides the model."""
import pytest

import agents.task.constants  # noqa: F401 — registers the light resolver
from core import runtime_config as rc

ENV = {"DEFAULT_PROVIDER": "openrouter", "DEFAULT_MODEL": "deepseek/deepseek-v4.1-flash",
       "HEAVY_MODEL": "deepseek/deepseek-v4-pro"}


def test_payload_model_wins_over_tier():
    assert rc.model_for({"model": "x/y", "tier": "heavy"}, "openrouter", env=ENV) == "x/y"


def test_stored_heavy_tier_resolves_on_the_operator_provider_only():
    assert rc.model_for({"tier": "heavy"}, "openrouter", env=ENV) == "deepseek/deepseek-v4-pro"
    assert rc.model_for({"tier": "heavy"}, "deepseek", env=ENV) is None


def test_unknown_stored_tier_is_default():
    assert rc.model_for({"tier": "ultra"}, "openrouter", env=ENV) is None


def test_money_class_still_heavy_without_a_tier():
    assert rc.model_for({"rig": "money_rail"}, "openrouter", env=ENV) == "deepseek/deepseek-v4-pro"


def test_light_equals_default_when_the_default_is_already_cheap():
    assert rc.tier_models("openrouter", ENV)["light"] == "deepseek/deepseek-v4.1-flash"


def test_light_uses_the_aux_map_and_ignores_aux_auto():
    env = dict(ENV, DEFAULT_MODEL="deepseek/deepseek-v4-pro", AUX_AUTO="false")
    assert rc.model_for({"tier": "light"}, "openrouter", env=env) == "deepseek/deepseek-v4.1-flash"


def test_tier_refusals():
    assert rc.tier_refusal("default", "anything", ENV) == ""
    assert rc.tier_refusal("heavy", "openrouter", ENV) == ""
    assert "not configured" in rc.tier_refusal("heavy", "openrouter",
                                                {k: v for k, v in ENV.items() if k != "HEAVY_MODEL"})
    assert "operator's provider" in rc.tier_refusal("heavy", "deepseek", ENV)
    assert "must be one of" in rc.tier_refusal("inherit", "openrouter", ENV)


@pytest.mark.parametrize("tier", rc.ROUTABLE_TIERS)
def test_every_routable_tier_is_a_model_tier(tier):
    assert tier in rc.MODEL_TIERS


def test_money_class_wins_over_a_stored_light_tier():
    """112 review: a light tier never moves money work off HEAVY_MODEL."""
    assert rc.model_for({"priority": "money", "tier": "light"}, "openrouter", env=ENV) == \
        "deepseek/deepseek-v4-pro"
    assert rc.model_for({"rig": "money_rail", "tier": "default"}, "openrouter", env=ENV) == \
        "deepseek/deepseek-v4-pro"


def test_light_refusal_names_the_real_cause():
    env = {"DEFAULT_PROVIDER": "openrouter"}
    assert "no DEFAULT_MODEL" in rc.tier_refusal("light", "openrouter", env)


def test_the_route_record_names_the_effective_tier(monkeypatch):
    from core import model_routing
    monkeypatch.setenv("HEAVY_MODEL", "deepseek/deepseek-v4-pro")
    seen = []
    monkeypatch.setattr(model_routing, "record_model_route", lambda **kw: seen.append(kw))
    model_routing.record_stored_tier(kind="goal", payload={"tier": "light", "priority": "money",
                                                           "tier_why": "cheap"},
                                     model="deepseek/deepseek-v4-pro", absent=False)
    assert seen[0]["tier"] == "heavy" and "money-class" in seen[0]["why"]


def test_a_money_run_with_no_heavy_model_is_recorded_on_the_default_tier(monkeypatch):
    """/status must not say "heavy" for money work that ran on the default model."""
    from core import model_routing
    monkeypatch.delenv("HEAVY_MODEL", raising=False)
    seen = []
    monkeypatch.setattr(model_routing, "record_model_route", lambda **kw: seen.append(kw))
    model_routing.record_stored_tier(kind="goal", payload={"tier": "default", "priority": "money"},
                                     model="deepseek/deepseek-v4.1-flash", absent=False)
    assert seen[0]["tier"] == "default" and "heavy not configured" in seen[0]["why"]
