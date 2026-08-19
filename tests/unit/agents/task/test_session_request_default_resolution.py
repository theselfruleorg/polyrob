"""SessionRequest provider/model defaults resolve via the operator's runtime
config (DEFAULT_PROVIDER pin > first keyed provider > openai/gpt-5 last resort)
instead of the hardcoded openai/gpt-5 dataclass literals.

Live-prod evidence (2026-08-14, Rob #1): every telegram-spawned session asked
for openai/gpt-5 on a box whose only working seat was the operator-pinned
zai-coding plan. With OpenRouter out of credits the fallback walk found
nothing, so EVERY inbound telegram turn (voice and text) died with
"no fallback providers could be initialized". The string path
``create_session(request=<text>)`` builds ``SessionRequest(task=text)`` and
inherited the bare literals — the DEFAULT_PROVIDER=zai-coding pin was never
consulted. This mirrors the goals/cron dispatch pattern
(``resolve_default_provider()[0]`` + ``get_default_model``), so surface-spawned
and autonomous sessions now share one default-resolution behavior.
"""

from agents.task_agent_lite import SessionRequest, _resolve_session_runtime
from modules.llm.llm_client_registry import get_default_model


def _clear_pins(monkeypatch):
    for var in ("CHAT_PROVIDER", "CHAT_MODEL", "DEFAULT_MODEL"):
        monkeypatch.delenv(var, raising=False)


def test_string_session_inherits_operator_pin(monkeypatch):
    """The telegram string path: SessionRequest(task=<text>) must land on the
    operator-pinned provider, not the openai/gpt-5 literal."""
    _clear_pins(monkeypatch)
    monkeypatch.setenv("DEFAULT_PROVIDER", "zai-coding")
    req = SessionRequest(task="hello")
    assert req.provider == "zai-coding"
    assert req.model == get_default_model("zai-coding")


def test_explicit_provider_and_model_untouched(monkeypatch):
    _clear_pins(monkeypatch)
    monkeypatch.setenv("DEFAULT_PROVIDER", "zai-coding")
    req = SessionRequest(task="hello", provider="openai", model="gpt-5")
    assert req.provider == "openai"
    assert req.model == "gpt-5"


def test_explicit_provider_fills_matching_model(monkeypatch):
    """A caller that pins only the provider gets that provider's registry
    default model — never another provider's model, never a bare None."""
    _clear_pins(monkeypatch)
    monkeypatch.setenv("DEFAULT_PROVIDER", "zai-coding")
    req = SessionRequest(task="hello", provider="anthropic")
    assert req.provider == "anthropic"
    assert req.model == get_default_model("anthropic")


def test_default_model_pin_honored(monkeypatch):
    _clear_pins(monkeypatch)
    monkeypatch.setenv("DEFAULT_PROVIDER", "zai-coding")
    monkeypatch.setenv("DEFAULT_MODEL", "glm-5.2")
    req = SessionRequest(task="hello")
    assert req.provider == "zai-coding"
    assert req.model == "glm-5.2"


def test_chat_model_pin_honored(monkeypatch):
    """CHAT_MODEL must be honored alongside CHAT_PROVIDER — the pin pair is
    read as a unit (``_resolve_chat_runtime`` reads CHAT_MODEL or DEFAULT_MODEL).
    Reading only DEFAULT_MODEL silently dropped an operator's CHAT_MODEL and
    served the registry default instead."""
    _clear_pins(monkeypatch)
    monkeypatch.delenv("DEFAULT_PROVIDER", raising=False)
    monkeypatch.setenv("CHAT_PROVIDER", "zai-coding")
    monkeypatch.setenv("CHAT_MODEL", "glm-5.2")
    req = SessionRequest(task="hello")
    assert req.provider == "zai-coding"
    assert req.model == "glm-5.2"


def test_chat_model_pin_beats_default_model(monkeypatch):
    """CHAT_ is the more specific pin tier — it wins over DEFAULT_, matching
    ``operator_provider_pin``'s CHAT_PROVIDER > DEFAULT_PROVIDER order."""
    _clear_pins(monkeypatch)
    monkeypatch.delenv("DEFAULT_PROVIDER", raising=False)
    monkeypatch.setenv("CHAT_PROVIDER", "zai-coding")
    monkeypatch.setenv("CHAT_MODEL", "glm-5.2")
    monkeypatch.setenv("DEFAULT_MODEL", "glm-4.6")
    req = SessionRequest(task="hello")
    assert req.model == "glm-5.2"


def test_task_session_config_defaults_resolve_provider(monkeypatch):
    """``TaskSessionConfig.defaults()`` must not bake openai/gpt-5 either — it
    is the config object the HTTP API path builds, and a future caller that
    does NOT overwrite the llm block would reproduce the 2026-08-14 outage."""
    from agents.task.config import TaskSessionConfig

    _clear_pins(monkeypatch)
    monkeypatch.setenv("DEFAULT_PROVIDER", "zai-coding")
    cfg = TaskSessionConfig.defaults()
    assert cfg.llm.provider == "zai-coding"
    assert cfg.llm.model == get_default_model("zai-coding")


def test_model_without_provider_detects_provider_from_registry(monkeypatch):
    """A caller that pins only the model (the A2A metadata path allows this)
    must get that model's OWN provider from the registry — not the operator
    pin, which would pair e.g. gpt-5 with a zai endpoint (400)."""
    _clear_pins(monkeypatch)
    monkeypatch.setenv("DEFAULT_PROVIDER", "zai-coding")
    req = SessionRequest(task="hello", model="gpt-5")
    assert req.provider == "openai"
    assert req.model == "gpt-5"


def test_unknown_model_without_provider_detects_via_registry_fuzzy_default(monkeypatch):
    """Pins the ACTUAL behavior for an unrecognized model id: the model
    registry's final fuzzy fallback resolves it to openai, so detection wins
    and the operator pin is NOT consulted. Documented explicitly because the
    opposite (fall through to the pin) is the intuitive-but-wrong reading —
    if the registry's fallback changes, this test must change with it."""
    _clear_pins(monkeypatch)
    monkeypatch.setenv("DEFAULT_PROVIDER", "zai-coding")
    req = SessionRequest(task="hello", model="totally-unknown-model-xyz")
    assert req.provider == "openai"
    assert req.model == "totally-unknown-model-xyz"


def test_no_pin_no_keys_last_resort_is_openai_gpt5():
    """Byte-compat floor: an unconfigured box keeps the historical default."""
    provider, model = _resolve_session_runtime(env={})
    assert provider == "openai"
    assert model == "gpt-5"


def test_openai_keyed_no_pin_keeps_historical_default():
    """With an OpenAI key present and no pin, the historical openai/gpt-5
    behavior is preserved (canonical first-keyed order)."""
    provider, model = _resolve_session_runtime(
        env={"OPENAI_API_KEY": "sk-" + "a" * 44}
    )
    assert provider == "openai"
    assert model == "gpt-5"
