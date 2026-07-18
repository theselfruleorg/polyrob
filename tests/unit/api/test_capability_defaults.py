"""/capabilities default (provider, model) must come from the registry policy.

The endpoint previously hardcoded ``getattr(config, 'model', 'x-ai/grok-4.1-fast')``
— a fallback literal the model registry had already marked deprecated (404s), and
one that leaked ``None`` when the attribute existed but was unset (audit T2,
2026-07-16).
"""
from api.task_http_api import _capability_defaults


class _Cfg:
    """Attribute-bearing stand-in for BotConfig."""

    def __init__(self, provider="__absent__", model="__absent__"):
        if provider != "__absent__":
            self.provider = provider
        if model != "__absent__":
            self.model = model


def test_explicit_config_wins():
    assert _capability_defaults(_Cfg("anthropic", "claude-sonnet-4-5")) == (
        "anthropic", "claude-sonnet-4-5")


def test_fallback_is_registry_policy_not_deprecated_literal():
    provider, model = _capability_defaults(_Cfg())
    from modules.llm.llm_client_registry import get_default_model
    assert provider == "openrouter"
    assert model == get_default_model("openrouter")
    assert model != "x-ai/grok-4.1-fast"  # deprecated (404s) — never advertise it


def test_none_valued_attrs_fall_back():
    # getattr(cfg, 'model', literal) returned None when the attr EXISTS but is
    # None — the old code then advertised None as the default model.
    provider, model = _capability_defaults(_Cfg(provider=None, model=None))
    assert provider and model
