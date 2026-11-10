"""chat_once provider/model resolution — 112 W6: the ONE chat ladder
(``core.model_pins.chat_runtime_for``): the owner's default > the operator pin > the first
keyed provider > openai. The old openai/gpt-5 preference is gone, so chat_once, a new
session and a recreated session agree. Model is always filled to match the provider.
"""
from agents.task_agent_lite import _resolve_chat_runtime


def test_two_keys_and_no_pin_agree_with_the_session_path():
    """112 F6: was ('openai','gpt-5') here and ('anthropic', …) on the session path."""
    from core.runtime_config import resolve_session_runtime
    env = {"OPENAI_API_KEY": "sk-" + "o" * 40, "ANTHROPIC_API_KEY": "sk-ant-" + "a" * 40}
    prov, model = _resolve_chat_runtime(env=env)
    assert prov == resolve_session_runtime(None, None, env=env)[0]
    assert model and (prov != "openai" or model)


def test_the_owners_default_reaches_chat_once(monkeypatch):
    import core.model_pins as mp
    monkeypatch.setattr(mp, "chat_pin_for", lambda uid, home_dir=None:
                        {"provider": "openrouter", "model": "z-ai/glm-5.2"} if uid == "u1" else None)
    assert _resolve_chat_runtime(env={}, user_id="u1") == ("openrouter", "z-ai/glm-5.2")


def test_first_keyed_provider_when_openai_absent():
    # Only an Anthropic key: chat must NOT try gpt-5/openai (the bug) — it routes
    # to anthropic with anthropic's registry default model. The key value must
    # pass looks_like_real_key (placeholder/too-short values are rejected by the
    # usability oracle, which would fall through to the openai last-resort).
    prov, model = _resolve_chat_runtime(env={"ANTHROPIC_API_KEY": "sk-ant-" + "a" * 40})
    assert prov == "anthropic"
    assert model and "gpt-5" not in model  # provider-appropriate model, not the openai default


def test_default_provider_env_pin_wins():
    prov, model = _resolve_chat_runtime(
        env={"DEFAULT_PROVIDER": "openrouter", "DEFAULT_MODEL": "z-ai/glm-5.2", "OPENAI_API_KEY": "x"}
    )
    assert prov == "openrouter"
    assert model == "z-ai/glm-5.2"


def test_default_provider_pin_without_model_fills_registry_default():
    prov, model = _resolve_chat_runtime(env={"DEFAULT_PROVIDER": "anthropic", "OPENAI_API_KEY": "x"})
    assert prov == "anthropic"
    assert model  # filled from the registry, not left as SessionRequest's gpt-5


def test_no_keys_falls_back_to_openai():
    prov, model = _resolve_chat_runtime(env={})
    assert prov == "openai"
    assert model


def test_one_chat_ladder():
    """112 W6: the chat resolver delegates to chat_runtime_for; no second preference list."""
    import inspect
    import agents.task.task_agent_support as tas
    src = inspect.getsource(tas._resolve_chat_runtime)
    assert "chat_runtime_for" in src and "cli_store_default" not in src


def test_billed_chat_once_never_starts_on_an_unpriced_owner_default():
    """112 review: chat_once falls back to the operator's model when the request is billed
    and the owner's default has no catalog price (as with_chat_pin does)."""
    import inspect
    import agents.task.task_agent_chat as tac
    src = inspect.getsource(tac)
    assert "is_billed() and stored_pin_refusal(_provider, _model)" in src
