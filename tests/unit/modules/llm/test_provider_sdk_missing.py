"""058 T7.2 — a missing provider SDK installs itself or names its extra.

After T1.3/T1.4 the Gemini and Anthropic SDKs are extras. Three seams touch
them, and each is pinned here with the SDK import BLOCKED:

1. ``create_llm_client`` used to import all eight client classes eagerly, so an
   OpenRouter deployment (prod) would have died at the first client build with
   an ImportError for a Gemini SDK it never uses. It now imports only the class
   the provider names, after ``ensure_provider`` for the SDK-bearing ones.
2. ``compat_clients`` imported ``AnthropicClient`` at module level, so the
   OpenAI-compatible path (Ollama, Groq, …) dragged in the Anthropic SDK.
3. ``create_chat_model``'s gemini/anthropic branches call ``ensure_provider``
   and let ``FeatureUnavailable`` propagate with its remedy instead of wrapping
   it into the generic "Failed to create chat model" ValueError.
"""
import builtins
import sys

import pytest

from core.lazy_deps import FeatureUnavailable


@pytest.fixture
def block_sdks(monkeypatch):
    """Make ``import google.generativeai`` / ``import anthropic`` fail as they
    would on a bare install, and evict any already-imported client modules so
    the module-level imports re-run."""
    blocked = ("google.generativeai", "google.ai.generativelanguage", "anthropic")
    for mod in list(sys.modules):
        if mod.startswith(blocked) or mod in (
            "modules.llm.gemini_client", "modules.llm.anthropic_client",
            "modules.llm.compat_clients", "modules.llm.compat_anthropic",
        ):
            monkeypatch.delitem(sys.modules, mod, raising=False)
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name.startswith(blocked):
            raise ImportError(f"No module named '{name}' (blocked by test)")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    import core.lazy_deps as ld
    monkeypatch.setattr(ld, "_dist_present",
                        lambda n: n not in {"google-generativeai", "anthropic"})
    monkeypatch.setattr(ld, "_run_installer", lambda cmd, **kw: pytest.fail("reached pip"))
    yield


def _config():
    from core.config import BotConfig
    return BotConfig()


def test_openrouter_client_builds_without_the_gemini_or_anthropic_sdk(block_sdks, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    from modules.llm.llm_client_registry import create_llm_client
    client = create_llm_client("openrouter", _config(), model_type="deepseek/deepseek-chat")
    assert type(client).__name__ == "OpenRouterClient"


def test_openai_compat_client_imports_without_the_anthropic_sdk(block_sdks):
    from modules.llm.compat_clients import OpenAICompatClient  # noqa: F401
    assert "modules.llm.anthropic_client" not in sys.modules


def test_gemini_client_refuses_naming_the_extra_when_lazy_is_off(block_sdks, monkeypatch):
    monkeypatch.setenv("LAZY_DEPS_ENABLED", "false")
    from modules.llm.llm_client_registry import create_llm_client
    with pytest.raises(FeatureUnavailable, match=r"polyrob\[gemini\]"):
        create_llm_client("gemini", _config())


def test_anthropic_compat_refuses_naming_the_extra_when_lazy_is_off(block_sdks, monkeypatch):
    monkeypatch.setenv("LAZY_DEPS_ENABLED", "false")
    from modules.llm.llm_client_registry import create_llm_client
    with pytest.raises(FeatureUnavailable, match=r"polyrob\[anthropic\]"):
        create_llm_client("zai-coding", _config())


def test_gemini_client_calls_ensure_once_then_imports(block_sdks, monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.delenv("LAZY_DEPS_ENABLED", raising=False)
    import core.lazy_deps as ld
    calls = []

    def fake_ensure(feature, *, prompt=True):
        calls.append(feature)
        raise FeatureUnavailable("pip exited 1 (simulated). Remedy: pip install 'polyrob[gemini]'")

    monkeypatch.setattr(ld, "ensure", fake_ensure)
    from modules.llm.llm_client_registry import create_llm_client
    with pytest.raises(FeatureUnavailable, match="simulated"):
        create_llm_client("gemini", _config())
    assert calls == ["provider.gemini"]


def test_factory_branch_lets_feature_unavailable_propagate(block_sdks, monkeypatch):
    """llm_factory wraps every failure in a generic ValueError; the remedy must
    survive that handler."""
    monkeypatch.setenv("LAZY_DEPS_ENABLED", "false")
    from modules.llm.llm_factory import create_chat_model

    class _Stub:  # never reaches the adapter
        cache_strategy = None

    with pytest.raises(FeatureUnavailable, match=r"polyrob\[gemini\]"):
        create_chat_model("gemini", "gemini-2.5-flash", 0.2, _Stub())
    with pytest.raises(FeatureUnavailable, match=r"polyrob\[anthropic\]"):
        create_chat_model("anthropic", "claude-sonnet-5", 0.2, _Stub())
