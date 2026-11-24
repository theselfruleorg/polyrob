"""The owner's saved model (`/model <name>`) is the model of every run nobody pinned —
goals, jobs and new chats — above the operator's ``DEFAULT_*`` pair: one
saved model drives everything."""
import pytest

import core.runtime_config as rc

ENV = {"DEFAULT_PROVIDER": "zai-coding", "DEFAULT_MODEL": "glm-5.3"}


@pytest.fixture
def env(monkeypatch):
    for k in ("DEFAULT_PROVIDER", "DEFAULT_MODEL", "CHAT_PROVIDER", "CHAT_MODEL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    keyed = {"zai-coding", "openrouter"}
    monkeypatch.setattr(rc, "usable_providers_with_credentials",
                        lambda e=None: [p for p in ("zai-coding", "openrouter") if p in keyed])
    return keyed


def _owner(monkeypatch, row):
    monkeypatch.setattr(rc, "_OWNER_MAIN_RESOLVER", lambda: row)


def test_no_saved_model_is_the_operator_pair(env, monkeypatch):
    _owner(monkeypatch, None)
    assert rc.resolve_default_provider()[:2] == ("zai-coding", "glm-5.3")
    assert rc.operator_model_for("zai-coding") == "glm-5.3"


def test_the_saved_model_drives_runs_nobody_pinned(env, monkeypatch):
    _owner(monkeypatch, {"provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash"})
    assert rc.resolve_default_provider()[:2] == ("openrouter", "deepseek/deepseek-v4.1-flash")
    # get_default_model(provider) reads this: a job on the saved provider runs the saved model
    assert rc.operator_model_for("openrouter") == "deepseek/deepseek-v4.1-flash"
    # another provider (a job's own pin, a fallback rung) keeps the operator's model for it
    assert rc.operator_model_for("zai-coding") == "glm-5.3"
    assert rc.main_provider_pin() == "openrouter"
    assert rc.operator_provider_pin() == "zai-coding"   # the raw operator pin is untouched


def test_a_saved_model_on_the_operator_provider_wins_there(env, monkeypatch):
    _owner(monkeypatch, {"provider": "zai-coding", "model": "glm-5.3-flash"})
    assert rc.operator_model_for("zai-coding") == "glm-5.3-flash"
    assert rc.resolve_default_provider()[:2] == ("zai-coding", "glm-5.3-flash")


def test_a_saved_provider_with_no_key_falls_back_to_the_operator(env, monkeypatch):
    _owner(monkeypatch, {"provider": "anthropic", "model": "claude-x"})
    assert rc.resolve_default_provider()[:2] == ("zai-coding", "glm-5.3")
    assert rc.operator_model_for("anthropic") is None


def test_an_explicit_env_mapping_is_a_what_if(env, monkeypatch):
    _owner(monkeypatch, {"provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash"})
    assert rc.resolve_default_provider(env=dict(ENV))[:2] == ("zai-coding", "glm-5.3")


def test_a_broken_resolver_is_the_operator_pair(env, monkeypatch):
    def boom():
        raise OSError("unreadable")
    monkeypatch.setattr(rc, "_OWNER_MAIN_RESOLVER", boom)
    assert rc.resolve_default_provider()[:2] == ("zai-coding", "glm-5.3")


def test_an_unpinned_job_prefers_the_saved_provider(env, monkeypatch):
    _owner(monkeypatch, {"provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash"})
    seen = {}
    monkeypatch.setattr(rc, "resolve_live_provider", lambda p=None, env=None: seen.setdefault("p", p))
    from cron.runner import resolve_job_provider
    resolve_job_provider({})
    assert seen["p"] == "openrouter"


def test_the_registered_resolver_reads_the_instance_owner(monkeypatch):
    import agents.task.constants  # noqa: F401 — registers the hook
    assert rc._OWNER_MAIN_RESOLVER is not None


def test_a_billed_request_never_runs_the_owner_choice(env, monkeypatch):
    from core import billing_context
    _owner(monkeypatch, {"provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash"})
    monkeypatch.setattr(billing_context, "is_billed", lambda: True)
    assert rc.resolve_default_provider()[:2] == ("zai-coding", "glm-5.3")
    assert rc.operator_model_for("openrouter") is None


def test_a_session_with_no_model_starts_on_the_saved_model(env, monkeypatch):
    _owner(monkeypatch, {"provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash"})
    assert rc.resolve_session_runtime() == ("openrouter", "deepseek/deepseek-v4.1-flash")
    assert rc.resolve_session_runtime("zai-coding", "glm-x") == ("zai-coding", "glm-x")


def test_tiers_follow_the_main_provider(env, monkeypatch):
    monkeypatch.setenv("HEAVY_MODEL", "glm-5.3-pro")             # a zai-coding id
    _owner(monkeypatch, None)
    assert rc.tier_models("zai-coding")["heavy"] == "glm-5.3-pro"
    _owner(monkeypatch, {"provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash"})
    tiers = rc.tier_models("openrouter")
    assert tiers["default"] == "deepseek/deepseek-v4.1-flash"
    assert "heavy" not in tiers                                  # never a zai id on openrouter
    assert rc.model_for({"priority": "money"}, "openrouter") is None   # -> the saved model
    assert rc.tier_models("zai-coding") == {}                    # no longer the main provider
    assert "main provider (openrouter)" in rc.tier_refusal("light", "zai-coding")
