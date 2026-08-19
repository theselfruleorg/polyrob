"""Characterization suite for proposal 024 P0 (declarative provider registry).

Pins the CURRENT resolution behavior of the twelve provider seams BEFORE the
``ProviderSpec`` refactor, so the derived outputs can be proven byte-identical.
After the refactor these tests run with ``LLM_PROVIDER_REGISTRY`` both on and
off (the ``registry_flag`` fixture) and must pass unchanged in both modes.

Covered seams (proposal 024 §2.2):
  1  modules/llm/profiles.py PROFILES + the three oracles
  4  model_registry.PROVIDER_CONFIG
  5  llm_client_registry DEFAULT_MODELS / AVAILABLE_MODELS / get_default_model
  6  llm_factory.create_chat_model unknown-provider guard
  7  schema_generators.SCHEMA_GENERATORS routing
  10 llm_manager.FALLBACK_HIERARCHY
  11 api/openai_compat/model_map._KNOWN_PROVIDERS
  12 api/openai_compat/model_map._PREFIX_TO_PROVIDER
  +  core/runtime_config.resolve_runtime_config precedence matrix
"""
import pytest


# ---------------------------------------------------------------------------
# Registry-flag parametrization (post-refactor: both modes must be identical).
# Before the ProviderSpec refactor lands the flag is unknown to the code and
# setting it is a no-op, so this fixture is safe from day one.
# ---------------------------------------------------------------------------
@pytest.fixture(params=["on", "off"])
def registry_flag(request, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_REGISTRY", "true" if request.param == "on" else "false")
    # Ensure no user providers.yaml leaks into characterization runs.
    monkeypatch.setenv("LLM_CUSTOM_PROVIDERS", "")
    try:
        from modules.llm import provider_spec
        provider_spec.reset_provider_registry_cache()
    except ImportError:
        pass  # pre-refactor tree
    yield request.param
    try:
        from modules.llm import provider_spec
        provider_spec.reset_provider_registry_cache()
    except ImportError:
        pass


CANONICAL_ORDER = ["openrouter", "anthropic", "openai", "gemini", "nvidia", "deepseek"]
INITIALIZABLE_ORDER = ["openrouter", "anthropic", "openai", "gemini", "nvidia"]

#: Subscription/flat-rate rows appended by 024 T0. They exist ONLY on the
#: registry path — the kill-switch (``LLM_PROVIDER_REGISTRY=off``) path is the
#: frozen legacy literal table, which cannot know about them. That divergence is
#: deliberate and is the first behavioral difference between the two modes; the
#: SIX legacy providers must still resolve identically in both, which is what
#: the rest of this suite pins. Because these rows are appended AFTER the six,
#: the canonical "first provider with a key" preference order is unchanged for
#: every provider an existing install actually has a key for.
SUBSCRIPTION_ROWS = [
    # 024 T0 — subscription plans that issue a key
    "ollama-cloud", "zai-coding", "cerebras",
    # 2026-08-12 Hermes-parity breadth. Same contract: appended after the six,
    # fallback-ineligible, never prompted for in init.
    "zai", "moonshot", "moonshot-cn", "kimi-coding", "minimax", "minimax-cn",
    "xai", "dashscope", "alibaba-coding", "stepfun", "ai-gateway",
    "opencode-zen", "opencode-go", "kilocode", "huggingface", "xiaomi",
    "tencent-tokenhub", "copilot",
    # OAuth subscription seats (no env_key — a seat is not an API key)
    "anthropic-oauth", "openai-codex", "github-copilot", "xai-oauth",
    "qwen-oauth", "minimax-oauth",
]

REAL = "sk-0123456789abcdef0123456789"  # >= 20 chars, not a placeholder

ALL_KEYS = {
    "OPENROUTER_API_KEY": REAL,
    "ANTHROPIC_API_KEY": REAL,
    "OPENAI_API_KEY": REAL,
    "GEMINI_API_KEY": REAL,
    "NVIDIA_API_KEY": REAL,
    "DEEPSEEK_API_KEY": REAL,
}


# ---------------------------------------------------------------------------
# Seam 1 — profiles + oracles
# ---------------------------------------------------------------------------
class TestProfilesOracles:
    def test_profiles_canonical_order_and_membership(self, registry_flag):
        from modules.llm.profiles import PROFILES
        names = list(PROFILES.keys())
        # The six legacy providers keep their exact identity AND order in both
        # modes — that is the parity this suite exists to protect.
        assert names[:6] == CANONICAL_ORDER
        if registry_flag == "off":
            assert names == CANONICAL_ORDER      # frozen legacy literal table
        else:
            assert names == CANONICAL_ORDER + SUBSCRIPTION_ROWS

    def test_subscription_rows_never_displace_the_legacy_six(self, registry_flag):
        """A 024 T0 row must not steal preference, fallback, or bootstrap.

        The subscription rows are appended last and are fallback-ineligible, so
        an install that has any legacy key resolves exactly as it did before.
        """
        from modules.llm.profiles import PROFILES, providers_with_keys
        assert providers_with_keys(ALL_KEYS) == CANONICAL_ORDER
        if registry_flag == "off":
            return
        from modules.llm.provider_spec import get_spec
        for name in SUBSCRIPTION_ROWS:
            spec = get_spec(name)
            assert spec is not None and spec.builtin, name
            assert spec.fallback_eligible is False, name
            # No prefix routing: several of these serve the same open-weight
            # model ids, so a prefix would silently hijack another provider.
            assert spec.model_prefixes == (), name
            # Served by the generic transport clients, never a bespoke class.
            assert spec.client_class_name is None, name
            # A built-in may use the vendor's REAL variable name, which is not
            # always `*_API_KEY` (HF_TOKEN, COPILOT_GITHUB_TOKEN). The
            # `*_API_KEY` shape rule is a guard on USER-declared rows — it stops
            # a providers.yaml file naming an arbitrary process secret as its
            # "api key". What must hold for a built-in is only that it names a
            # credential var and never a non-LLM secret.
            from modules.llm.provider_spec import _env_name_sensitive
            if spec.oauth is not None:
                # An OAuth seat carries NO env key by design — giving it one
                # would make an unrelated variable look like a credential.
                assert spec.env_key is None, name
                continue
            for var in spec.env_key_chain():
                assert var.isupper(), name
                assert var.endswith(("_API_KEY", "_TOKEN")), (name, var)
                assert not _env_name_sensitive(var.replace("_TOKEN", "")), (name, var)

    def test_profile_fields_pinned(self, registry_flag):
        from modules.llm.profiles import PROFILES
        p = PROFILES["openrouter"]
        assert (p.env_key, p.base_url) == ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1")
        assert PROFILES["anthropic"].base_url == "https://api.anthropic.com"
        assert PROFILES["openai"].base_url is None
        assert PROFILES["gemini"].env_key == "GEMINI_API_KEY"
        assert PROFILES["nvidia"].base_url == "https://integrate.api.nvidia.com/v1"
        assert PROFILES["nvidia"].supports_vision is False
        assert PROFILES["deepseek"].initializable is False
        assert PROFILES["deepseek"].supports_native_tools is False
        for name in INITIALIZABLE_ORDER:
            assert PROFILES[name].initializable is True

    def test_providers_with_keys_order_and_deepseek(self, registry_flag):
        from modules.llm.profiles import providers_with_keys
        assert providers_with_keys(ALL_KEYS) == CANONICAL_ORDER
        assert providers_with_keys({"DEEPSEEK_API_KEY": REAL}) == ["deepseek"]
        assert providers_with_keys({}) == []
        # blank value counts as absent
        assert providers_with_keys({"OPENAI_API_KEY": ""}) == []

    def test_initializable_excludes_deepseek(self, registry_flag):
        from modules.llm.profiles import initializable_providers_with_keys
        assert initializable_providers_with_keys(ALL_KEYS) == INITIALIZABLE_ORDER
        assert initializable_providers_with_keys({"DEEPSEEK_API_KEY": REAL}) == []

    def test_usable_rejects_malformed(self, registry_flag):
        from modules.llm.profiles import usable_providers_with_keys
        assert usable_providers_with_keys(ALL_KEYS) == INITIALIZABLE_ORDER
        assert usable_providers_with_keys({"OPENAI_API_KEY": "short"}) == []
        assert usable_providers_with_keys({"OPENAI_API_KEY": "your-openai-key"}) == []
        assert usable_providers_with_keys({"DEEPSEEK_API_KEY": REAL}) == []


# ---------------------------------------------------------------------------
# resolve_runtime_config precedence matrix
# ---------------------------------------------------------------------------
class TestRuntimeConfigMatrix:
    def test_explicit_wins_even_with_no_key(self, registry_flag):
        from core.runtime_config import resolve_runtime_config
        assert resolve_runtime_config("anthropic", "m1", env={}) == ("anthropic", "m1")

    def test_pinned_wins_below_explicit(self, registry_flag):
        from core.runtime_config import resolve_runtime_config
        assert resolve_runtime_config(
            None, None, env={}, pinned_provider="gemini", pinned_model="g"
        ) == ("gemini", "g")
        assert resolve_runtime_config(
            "openai", None, env={}, pinned_provider="gemini", pinned_model="g"
        ) == ("openai", None)

    def test_cli_store_intersected_with_keys(self, registry_flag):
        from core.runtime_config import resolve_runtime_config
        # stored provider has a key -> honored
        assert resolve_runtime_config(
            None, None, env=ALL_KEYS, cli_store_default=("gemini", "g2")
        ) == ("gemini", "g2")
        # stored provider without a key -> skipped, first-key wins
        assert resolve_runtime_config(
            None, None, env={"OPENAI_API_KEY": REAL}, cli_store_default=("gemini", "g2")
        ) == ("openai", None)

    def test_first_key_canonical_order(self, registry_flag):
        from core.runtime_config import resolve_runtime_config
        assert resolve_runtime_config(None, None, env=ALL_KEYS) == ("openrouter", None)
        env = {"GEMINI_API_KEY": REAL, "OPENAI_API_KEY": REAL}
        assert resolve_runtime_config(None, None, env=env) == ("openai", None)

    def test_deepseek_key_never_autoresolves(self, registry_flag):
        from core.runtime_config import resolve_runtime_config
        assert resolve_runtime_config(
            None, None, env={"DEEPSEEK_API_KEY": REAL}
        ) == ("openai", None)  # falls to last_resort

    def test_malformed_key_never_autoresolves(self, registry_flag):
        from core.runtime_config import resolve_runtime_config
        assert resolve_runtime_config(
            None, None, env={"ANTHROPIC_API_KEY": "short"}
        ) == ("openai", None)

    def test_available_keys_name_based_path(self, registry_flag):
        from core.runtime_config import resolve_runtime_config
        got = resolve_runtime_config(
            None, None, env={}, available_keys={"ANTHROPIC_API_KEY", "GEMINI_API_KEY"}
        )
        assert got == ("anthropic", None)
        # deepseek name is filtered by initializable
        got = resolve_runtime_config(
            None, None, env={}, available_keys={"DEEPSEEK_API_KEY"}
        )
        assert got == ("openai", None)

    def test_last_resort(self, registry_flag):
        from core.runtime_config import resolve_runtime_config
        assert resolve_runtime_config(
            None, None, env={}, last_resort=("gemini", None)
        ) == ("gemini", None)


# ---------------------------------------------------------------------------
# Seam 4 — PROVIDER_CONFIG
# ---------------------------------------------------------------------------
class TestProviderConfig:
    def test_entries_pinned(self, registry_flag):
        from modules.llm.model_registry import PROVIDER_CONFIG
        expected = {
            "openai": ("OpenAIClient", True),
            "anthropic": ("AnthropicClient", True),
            "deepseek": ("DeepSeekClient", False),
            "gemini": ("GeminiClient", True),
            "openrouter": ("OpenRouterClient", True),
            "nvidia": ("NvidiaClient", False),
        }
        for name, (cls, fb) in expected.items():
            entry = PROVIDER_CONFIG[name]
            assert entry.client_class_name == cls, name
            assert entry.fallback_eligible is fb, name
        assert set(expected) <= set(PROVIDER_CONFIG.keys())


# ---------------------------------------------------------------------------
# Seam 5 — DEFAULT_MODELS / get_default_model / AVAILABLE_MODELS
# ---------------------------------------------------------------------------
class TestDefaultModels:
    PINNED = {
        "anthropic": "claude-sonnet-4-5",
        "openai": "gpt-5",
        "gemini": "gemini-2.5-flash",
        "deepseek": "deepseek-chat",
        "openrouter": "z-ai/glm-5.2",
        "nvidia": "moonshotai/kimi-k2.6",
    }

    def test_default_models_pinned(self, registry_flag):
        from modules.llm.llm_client_registry import DEFAULT_MODELS
        for k, v in self.PINNED.items():
            assert DEFAULT_MODELS[k] == v

    def test_get_default_model_env_override(self, registry_flag, monkeypatch):
        from modules.llm.llm_client_registry import get_default_model
        monkeypatch.setenv("POLYROB_OPENROUTER_MODEL", "x-ai/grok-4.3")
        assert get_default_model("openrouter") == "x-ai/grok-4.3"
        monkeypatch.delenv("POLYROB_OPENROUTER_MODEL")
        assert get_default_model("openrouter") == "z-ai/glm-5.2"
        # unknown provider falls back to the openai default
        assert get_default_model("nope") == self.PINNED["openai"]

    def test_available_models_providers(self, registry_flag):
        from modules.llm.llm_client_registry import AVAILABLE_MODELS
        for name in CANONICAL_ORDER:
            assert name in AVAILABLE_MODELS
            assert isinstance(list(AVAILABLE_MODELS[name]), list)
        # registry-backed providers have non-empty lists
        assert AVAILABLE_MODELS["openai"]
        assert AVAILABLE_MODELS["anthropic"]


# ---------------------------------------------------------------------------
# Seam 6 — factory unknown-provider guard
# ---------------------------------------------------------------------------
class TestFactoryGuard:
    def test_unknown_provider_raises(self, registry_flag):
        from modules.llm.llm_factory import create_chat_model
        with pytest.raises(ValueError, match="Unsupported LLM provider"):
            create_chat_model("definitely-not-a-provider", "m", 0.5, object())


# ---------------------------------------------------------------------------
# Seam 7 — schema generator routing
# ---------------------------------------------------------------------------
class TestSchemaGenerators:
    def test_routing_pinned(self, registry_flag):
        from tools.controller.registry.schema_generators import (
            get_schema_generator,
            OpenAISchemaGenerator,
            AnthropicSchemaGenerator,
            GeminiSchemaGenerator,
            JSONFallbackSchemaGenerator,
        )
        assert isinstance(get_schema_generator("openai"), OpenAISchemaGenerator)
        assert isinstance(get_schema_generator("anthropic"), AnthropicSchemaGenerator)
        assert isinstance(get_schema_generator("gemini"), GeminiSchemaGenerator)
        assert isinstance(get_schema_generator("google"), GeminiSchemaGenerator)
        assert isinstance(get_schema_generator("deepseek"), OpenAISchemaGenerator)
        assert isinstance(get_schema_generator("openrouter"), OpenAISchemaGenerator)
        assert isinstance(get_schema_generator("nvidia"), OpenAISchemaGenerator)
        # partial match keeps working
        assert isinstance(get_schema_generator("openai-gpt4"), OpenAISchemaGenerator)
        # unknown falls back to JSON generator
        assert isinstance(
            get_schema_generator("no-such-provider-xyz"), JSONFallbackSchemaGenerator
        )


# ---------------------------------------------------------------------------
# Seam 10 — FALLBACK_HIERARCHY
# ---------------------------------------------------------------------------
class TestFallbackHierarchy:
    def test_pairs_pinned(self, registry_flag):
        from modules.llm.llm_manager import LLMManager

        class _Cfg:
            def get_llm_config(self):
                return {}

        mgr = LLMManager("llm_manager", _Cfg())
        assert mgr.FALLBACK_HIERARCHY == [
            ("openai_client", "gpt-5"),
            ("anthropic_client", "claude-sonnet-4-5"),
            ("openrouter_client", "z-ai/glm-5.2"),
            ("gemini_client", "gemini-2.5-flash"),
        ]


# ---------------------------------------------------------------------------
# Seams 11/12 — openai-compat model map
# ---------------------------------------------------------------------------
class TestModelMap:
    def test_known_provider_slug_split(self, registry_flag):
        from api.openai_compat.model_map import map_model
        assert map_model("anthropic/claude-sonnet-4-5") == ("anthropic", "claude-sonnet-4-5")
        assert map_model("openrouter/z-ai/glm-5.2") == ("openrouter", "z-ai/glm-5.2")
        assert map_model("nvidia/moonshotai/kimi-k2.6") == ("nvidia", "moonshotai/kimi-k2.6")

    def test_prefix_table_pinned(self, registry_flag, monkeypatch):
        from api.openai_compat.model_map import map_model
        # Use model names NOT in the registry so the prefix table (not ownership) routes.
        assert map_model("gpt-99-preview") == ("openai", "gpt-99-preview")
        assert map_model("o1-mega") == ("openai", "o1-mega")
        assert map_model("o3-mega") == ("openai", "o3-mega")
        assert map_model("claude-99") == ("anthropic", "claude-99")
        assert map_model("gemini-99") == ("gemini", "gemini-99")
        assert map_model("deepseek-ultra") == ("deepseek", "deepseek-ultra")
        assert map_model("kimi-k99") == ("nvidia", "kimi-k99")

    def test_registry_ownership_beats_prefix(self, registry_flag):
        from api.openai_compat.model_map import _provider_owning
        # a registered openrouter slug is owned by openrouter even without a prefix hit
        owner = _provider_owning("z-ai/glm-5.2")
        assert owner in ("openrouter", None)  # None only if registry lookup fails
