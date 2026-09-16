"""A RESUMED chat session pinned to a dead provider must re-route, not go mute.

Live prod, 2026-09-16. The owner's Telegram session was created on 2026-09-11 and
its `metadata.json` froze `{"provider": "zai-coding", "model": "glm-5"}`.
`SessionManager._load_sessions_from_disk` loads that record back on every restart,
so each owner turn re-requested glm-5 from an account whose weekly quota was
exhausted (429/1310). The operator had already re-pinned the box to a funded
provider; new sessions used it, the owner's session did not. Symptom: the agent
reported itself online and answered nothing.

`resolve_live_provider` was written for this exact class ("a pin says prefer this,
not: and never run again if this dies") and was wired into goal dispatch and the
status snapshot — but NOT into `resolve_session_runtime`, which is the path every
chat/telegram turn takes. These tests close that gap.

⚠️ The stale MODEL must travel with the stale provider. Carrying `glm-5` onto
OpenRouter is not a recovery: the registry's pattern fallback maps it to
`z-ai/glm-5.2`, a different model on a metered account.
"""
import pytest

import core.runtime_config as rc
from core.runtime_config import resolve_session_runtime


@pytest.fixture
def _two_providers(monkeypatch):
    monkeypatch.setattr(rc, "usable_providers_with_credentials",
                        lambda env=None: ["zai-coding", "openrouter"], raising=False)


def _dead(*names):
    dead = set(names)

    def _active(provider=None):
        return bool(dead) if provider is None else provider in dead
    return _active


def test_a_live_stored_pin_is_untouched(_two_providers, monkeypatch):
    monkeypatch.setattr(rc, "_sentinel_active", _dead(), raising=False)
    provider, model = resolve_session_runtime("zai-coding", "glm-5", env={})
    assert (provider, model) == ("zai-coding", "glm-5")


def test_a_dead_stored_pin_reroutes_to_the_operator_pin(_two_providers, monkeypatch):
    monkeypatch.setattr(rc, "_sentinel_active", _dead("zai-coding"), raising=False)
    env = {"DEFAULT_PROVIDER": "openrouter",
           "DEFAULT_MODEL": "deepseek/deepseek-v4.1-flash"}
    provider, model = resolve_session_runtime("zai-coding", "glm-5", env=env)
    assert provider == "openrouter"
    assert model == "deepseek/deepseek-v4.1-flash", "the dead provider's model must not survive"


def test_the_stale_model_never_rides_onto_the_new_provider(_two_providers, monkeypatch):
    # No operator model pin: the model must come back None so the caller fills it
    # from the registry for the NEW provider — never the dead provider's literal.
    monkeypatch.setattr(rc, "_sentinel_active", _dead("zai-coding"), raising=False)
    provider, model = resolve_session_runtime("zai-coding", "glm-5",
                                              env={"DEFAULT_PROVIDER": "openrouter"})
    assert provider == "openrouter"
    assert model != "glm-5"


def test_everything_dead_keeps_the_callers_pin(_two_providers, monkeypatch):
    # Nothing can serve. Re-routing to another dead provider would only swap one
    # failure for another; the turn should fail honestly against what it asked for.
    monkeypatch.setattr(rc, "_sentinel_active", _dead("zai-coding", "openrouter"),
                        raising=False)
    provider, model = resolve_session_runtime("zai-coding", "glm-5", env={})
    assert (provider, model) == ("zai-coding", "glm-5")


def test_no_stored_pin_is_unchanged(_two_providers, monkeypatch):
    # The fill-the-blanks path must behave exactly as before.
    monkeypatch.setattr(rc, "_sentinel_active", _dead(), raising=False)
    provider, model = resolve_session_runtime(
        None, None, env={"DEFAULT_PROVIDER": "openrouter",
                         "DEFAULT_MODEL": "deepseek/deepseek-v4.1-flash"})
    assert (provider, model) == ("openrouter", "deepseek/deepseek-v4.1-flash")


def test_resolver_failure_is_fail_open(_two_providers, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("sentinel store unreadable")
    monkeypatch.setattr(rc, "_sentinel_active", _boom, raising=False)
    # Fail-open contract: session creation must never die on a config problem.
    provider, model = resolve_session_runtime("zai-coding", "glm-5", env={})
    assert provider == "zai-coding"
