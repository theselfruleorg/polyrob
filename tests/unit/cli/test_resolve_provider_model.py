import pytest

import cli.config_store as cs


@pytest.fixture(autouse=True)
def _clear_pin_env(monkeypatch):
    # resolve_provider_model reads CHAT_/DEFAULT_ provider/model pins from os.environ.
    # Other tests' load_env() can leak a machine's ~/.polyrob/.env values (e.g. a
    # stale DEFAULT_PROVIDER) into the process env, which would make these unit tests
    # pollution-sensitive. Clear the pins so each test controls its own inputs; the
    # tests that exercise the pin path set them explicitly after this runs.
    for _k in ("DEFAULT_PROVIDER", "DEFAULT_MODEL", "CHAT_PROVIDER", "CHAT_MODEL"):
        monkeypatch.delenv(_k, raising=False)


def test_explicit_cli_args_win(monkeypatch):
    monkeypatch.setattr(cs, "get_default_model", lambda: (None, None))
    provider, model = cs.resolve_provider_model("anthropic", "claude-x", available_keys=set())
    assert provider == "anthropic"
    assert model == "claude-x"


def test_stored_default_used_when_no_cli_arg(monkeypatch):
    monkeypatch.setattr(cs, "get_default_model", lambda: ("openai", "gpt-5"))
    provider, model = cs.resolve_provider_model(None, None, available_keys={"OPENAI_API_KEY"})
    assert provider == "openai"
    assert model == "gpt-5"


def test_falls_back_to_provider_with_present_key_not_gemini(monkeypatch):
    monkeypatch.setattr(cs, "get_default_model", lambda: (None, None))
    provider, model = cs.resolve_provider_model(None, None, available_keys={"ANTHROPIC_API_KEY"})
    assert provider == "anthropic"


def test_last_resort_is_gemini_when_no_keys(monkeypatch):
    monkeypatch.setattr(cs, "get_default_model", lambda: (None, None))
    provider, model = cs.resolve_provider_model(None, None, available_keys=set())
    assert provider == "gemini"


def test_nvidia_key_autodetected(monkeypatch):
    monkeypatch.setattr(cs, "get_default_model", lambda: (None, None))
    provider, model = cs.resolve_provider_model(None, None, available_keys={"NVIDIA_API_KEY"})
    assert provider == "nvidia"


def test_cli_provider_without_model_drops_stored_model(monkeypatch):
    # explicit --provider but no --model: the stored model (for a different provider)
    # must NOT leak through; caller fills from the registry instead.
    monkeypatch.setattr(cs, "get_default_model", lambda: ("anthropic", "claude-x"))
    provider, model = cs.resolve_provider_model("openai", None, available_keys=set())
    assert provider == "openai"
    assert model is None


def test_stale_cli_json_keyless_provider_skipped(monkeypatch):
    # Phase 0c: a stored default (~/.rob/cli.json) whose provider has NO key present
    # must be skipped — fall through to the first provider that DOES have a key.
    # Regression for the live 'nvidia/kimi'-with-only-OpenRouter-key bug.
    monkeypatch.setattr(cs, "get_default_model", lambda: ("nvidia", "moonshotai/kimi-k2.6"))
    provider, model = cs.resolve_provider_model(
        None, None, available_keys={"OPENROUTER_API_KEY"}
    )
    assert provider == "openrouter"
    assert model is None  # stale stored model dropped with the stale provider


def test_explicit_keyless_provider_still_wins(monkeypatch):
    # Intersection is explicit-EXEMPT: a user who types -p anthropic gets anthropic
    # even with no key (they'll see a clean downstream auth error).
    monkeypatch.setattr(cs, "get_default_model", lambda: (None, None))
    provider, _ = cs.resolve_provider_model("anthropic", None, available_keys=set())
    assert provider == "anthropic"


def test_keyed_stored_default_still_wins_over_first_key(monkeypatch):
    # A stored default that DOES have a key must win over the canonical first-key pick.
    monkeypatch.setattr(cs, "get_default_model", lambda: ("openai", "gpt-5"))
    provider, model = cs.resolve_provider_model(
        None, None, available_keys={"OPENAI_API_KEY", "ANTHROPIC_API_KEY"}
    )
    assert provider == "openai"
    assert model == "gpt-5"


def test_deepseek_key_does_not_autoresolve(monkeypatch):
    # A DEEPSEEK_API_KEY alone must NOT auto-resolve to provider=deepseek (disabled
    # direct client → would hard-crash the manager). Falls to the gemini last resort.
    monkeypatch.setattr(cs, "get_default_model", lambda: (None, None))
    provider, _ = cs.resolve_provider_model(None, None, available_keys={"DEEPSEEK_API_KEY"})
    assert provider == "gemini"


def test_default_provider_env_pins_resolution(monkeypatch):
    # `polyrob init` writes DEFAULT_PROVIDER/DEFAULT_MODEL to ~/.polyrob/.env. The CLI
    # resolver (run/chat/doctor) must honor them — they were silently ignored before
    # (only the chat_once/A2A path read them), so the wizard's choice was dead.
    monkeypatch.setattr(cs, "get_default_model", lambda: (None, None))
    monkeypatch.delenv("CHAT_PROVIDER", raising=False)
    monkeypatch.delenv("CHAT_MODEL", raising=False)
    monkeypatch.setenv("DEFAULT_PROVIDER", "anthropic")
    monkeypatch.setenv("DEFAULT_MODEL", "claude-sonnet-4-5")
    # anthropic absent from keys, openrouter present — the pin is exempt (Agent-4 repro)
    provider, model = cs.resolve_provider_model(None, None, available_keys={"OPENROUTER_API_KEY"})
    assert provider == "anthropic"
    assert model == "claude-sonnet-4-5"


def test_chat_env_overrides_default_env(monkeypatch):
    # CHAT_PROVIDER/CHAT_MODEL take precedence over DEFAULT_* (mirrors task_agent_lite).
    monkeypatch.setattr(cs, "get_default_model", lambda: (None, None))
    monkeypatch.setenv("DEFAULT_PROVIDER", "anthropic")
    monkeypatch.setenv("DEFAULT_MODEL", "claude-sonnet-4-5")
    monkeypatch.setenv("CHAT_PROVIDER", "openai")
    monkeypatch.setenv("CHAT_MODEL", "gpt-5")
    provider, model = cs.resolve_provider_model(None, None, available_keys=set())
    assert provider == "openai"
    assert model == "gpt-5"


def test_explicit_flag_beats_default_env(monkeypatch):
    # An explicit --provider must still win over the DEFAULT_PROVIDER env pin.
    monkeypatch.setattr(cs, "get_default_model", lambda: (None, None))
    monkeypatch.setenv("DEFAULT_PROVIDER", "anthropic")
    monkeypatch.delenv("CHAT_PROVIDER", raising=False)
    provider, _ = cs.resolve_provider_model("openai", None, available_keys=set())
    assert provider == "openai"


def test_model_without_provider_infers_owning_provider(monkeypatch):
    # `run -m gpt-5` with only an OpenRouter key must resolve provider=openai (gpt-5's
    # registry owner), not openrouter (which needs 'openai/gpt-5') — the incoherent
    # pair that produced an opaque failure before.
    monkeypatch.setattr(cs, "get_default_model", lambda: (None, None))
    monkeypatch.delenv("DEFAULT_PROVIDER", raising=False)
    monkeypatch.delenv("CHAT_PROVIDER", raising=False)
    provider, model = cs.resolve_provider_model(None, "gpt-5", available_keys={"OPENROUTER_API_KEY"})
    assert provider == "openai"
    assert model == "gpt-5"


def test_slash_model_without_provider_infers_openrouter(monkeypatch):
    monkeypatch.setattr(cs, "get_default_model", lambda: (None, None))
    monkeypatch.delenv("DEFAULT_PROVIDER", raising=False)
    monkeypatch.delenv("CHAT_PROVIDER", raising=False)
    provider, model = cs.resolve_provider_model(
        None, "z-ai/glm-5.2", available_keys={"OPENROUTER_API_KEY", "OPENAI_API_KEY"}
    )
    assert provider == "openrouter"
    assert model == "z-ai/glm-5.2"


def test_unknown_model_keeps_resolved_provider(monkeypatch):
    monkeypatch.setattr(cs, "get_default_model", lambda: (None, None))
    monkeypatch.delenv("DEFAULT_PROVIDER", raising=False)
    monkeypatch.delenv("CHAT_PROVIDER", raising=False)
    provider, model = cs.resolve_provider_model(
        None, "totally-custom-xyz", available_keys={"OPENROUTER_API_KEY"}
    )
    assert provider == "openrouter"  # unknown model -> don't override the resolution
    assert model == "totally-custom-xyz"


def test_explicit_provider_plus_model_not_overridden_by_inference(monkeypatch):
    # An explicit --provider must not be second-guessed by model inference.
    monkeypatch.setattr(cs, "get_default_model", lambda: (None, None))
    provider, model = cs.resolve_provider_model("openrouter", "gpt-5", available_keys=set())
    assert provider == "openrouter"
    assert model == "gpt-5"


def test_check_provider_model_rejects_unknown_provider():
    known, warning = cs.check_provider_model("nope", "whatever")
    assert known is False


def test_check_provider_model_warns_unknown_model():
    known, warning = cs.check_provider_model("openai", "not-a-real-openai-model")
    assert known is True
    assert warning  # non-empty warning


def test_check_provider_model_ok_for_known_pair():
    known, warning = cs.check_provider_model("openai", "gpt-5")
    assert known is True
    assert warning == ""


def test_explicit_model_without_provider_is_preserved(monkeypatch):
    # `polyrob run -m custom-model` (no -p): the explicit model must win over the stored
    # model of the resolved provider.
    monkeypatch.setattr(cs, "get_default_model", lambda: ("openai", "gpt-5"))
    provider, model = cs.resolve_provider_model(
        None, "custom-model", available_keys={"OPENAI_API_KEY"}
    )
    assert provider == "openai"
    assert model == "custom-model"
