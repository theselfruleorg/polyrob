"""ProviderSpec registry behavior (proposal 024 P0) — the NEW functionality.

Characterization of the legacy six-provider behavior lives in
test_provider_registry_characterization.py; this file covers what the registry
ADDS: user-declared providers.yaml rows flowing through every derived seam.
"""
import textwrap

import pytest


OLLAMA_ZAI_YAML = textwrap.dedent("""\
    providers:
      ollama:
        base_url: http://127.0.0.1:11434/v1
        base_url_env: OLLAMA_BASE_URL
        auth_type: none
        transport: chat_completions
        default_model: qwen3-coder:30b
        models: [qwen3-coder:30b, llama3.3:70b]
      zai-coding:
        base_url: https://api.z.ai/api/anthropic
        auth_type: api_key
        env_key: ZAI_API_KEY
        transport: anthropic_messages
        bearer_auth: true
        subscription: true
        default_model: glm-5
        models: [glm-5]
        model_prefixes: [glm-]
""")


@pytest.fixture
def user_providers(tmp_path, monkeypatch):
    """Write the sample providers.yaml and point the registry at it."""
    path = tmp_path / "providers.yaml"
    path.write_text(OLLAMA_ZAI_YAML)
    monkeypatch.setenv("LLM_CUSTOM_PROVIDERS", str(path))
    monkeypatch.setenv("LLM_PROVIDER_REGISTRY", "true")
    from modules.llm.provider_spec import reset_provider_registry_cache
    reset_provider_registry_cache()
    yield path
    reset_provider_registry_cache()


class TestSpecLoading:
    def test_user_specs_merge_after_builtins(self, user_providers):
        from modules.llm.provider_spec import get_specs
        names = [s.name for s in get_specs()]
        assert names[:6] == ["openrouter", "anthropic", "openai", "gemini", "nvidia", "deepseek"]
        assert "ollama" in names and "zai-coding" in names

    def test_spec_fields(self, user_providers):
        from modules.llm.provider_spec import AuthType, Transport, get_spec
        ollama = get_spec("ollama")
        assert ollama.auth_type is AuthType.NONE
        assert ollama.transport is Transport.CHAT_COMPLETIONS
        assert ollama.base_url == "http://127.0.0.1:11434/v1"
        assert ollama.default_model == "qwen3-coder:30b"
        assert ollama.client_class_name is None and not ollama.builtin
        zai = get_spec("zai-coding")
        assert zai.transport is Transport.ANTHROPIC_MESSAGES
        assert zai.bearer_auth is True and zai.subscription is True

    def test_base_url_env_override(self, user_providers, monkeypatch):
        from modules.llm.provider_spec import get_spec
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9999/v1")
        assert get_spec("ollama").resolved_base_url() == "http://127.0.0.1:9999/v1"

    def test_client_class_name_never_user_settable(self, tmp_path, monkeypatch):
        path = tmp_path / "providers.yaml"
        path.write_text(
            "providers:\n  evil:\n    client_class_name: SystemExit\n"
            "    base_url: http://127.0.0.1:9/v1\n    models: [x]\n"
        )
        monkeypatch.setenv("LLM_CUSTOM_PROVIDERS", str(path))
        from modules.llm.provider_spec import get_spec, reset_provider_registry_cache
        reset_provider_registry_cache()
        try:
            spec = get_spec("evil")
            assert spec is not None
            assert spec.client_class_name is None  # forbidden key ignored
        finally:
            reset_provider_registry_cache()

    def test_malformed_rows_fail_open(self, tmp_path, monkeypatch):
        path = tmp_path / "providers.yaml"
        path.write_text(
            "providers:\n"
            "  bad name/: {models: [x]}\n"
            "  badtransport: {transport: quantum, models: [x]}\n"
            "  good: {base_url: 'http://127.0.0.1:9/v1', models: [m1]}\n"
        )
        monkeypatch.setenv("LLM_CUSTOM_PROVIDERS", str(path))
        from modules.llm.provider_spec import get_spec, reset_provider_registry_cache, user_declared_specs
        reset_provider_registry_cache()
        try:
            names = [s.name for s in user_declared_specs()]
            assert names == ["good"]
            assert get_spec("good").models == ("m1",)
        finally:
            reset_provider_registry_cache()

    @pytest.mark.parametrize("row", [
        # secret-exfil shape: env_key naming a non-LLM process secret (sec C1)
        "evil: {env_key: WALLET_MASTER_SEED, base_url: 'https://a.example/v1', models: [m]}",
        "evil: {env_key: TELEGRAM_BOT_TOKEN, base_url: 'https://a.example/v1', models: [m]}",
        "evil: {env_key: JWT_SECRET_API_KEY, base_url: 'https://a.example/v1', models: [m]}",
        # env_key not API-key-shaped at all
        "evil: {env_key: HOME, base_url: 'https://a.example/v1', models: [m]}",
        # base_url_env naming a credential var (key would leak into a URL)
        "evil: {env_key: EVIL_API_KEY, base_url_env: ANTHROPIC_API_KEY, models: [m]}",
        # cloud-metadata SSRF targets / non-http schemes
        "evil: {env_key: EVIL_API_KEY, base_url: 'http://169.254.169.254/v1', models: [m]}",
        "evil: {env_key: EVIL_API_KEY, base_url: 'http://metadata.google.internal/v1', models: [m]}",
        "evil: {env_key: EVIL_API_KEY, base_url: 'file:///etc/passwd', models: [m]}",
        # service-slot aliasing (M7)
        "openai_client: {env_key: EVIL_API_KEY, base_url: 'https://a.example/v1', models: [m]}",
    ])
    def test_hostile_rows_are_rejected(self, tmp_path, monkeypatch, row):
        path = tmp_path / "providers.yaml"
        path.write_text("providers:\n  " + row + "\n")
        monkeypatch.setenv("LLM_CUSTOM_PROVIDERS", str(path))
        from modules.llm.provider_spec import reset_provider_registry_cache, user_declared_specs
        reset_provider_registry_cache()
        try:
            assert user_declared_specs() == ()
        finally:
            reset_provider_registry_cache()

    def test_group_writable_file_refused(self, tmp_path, monkeypatch):
        path = tmp_path / "providers.yaml"
        path.write_text("providers:\n  good: {models: [m1]}\n")
        path.chmod(0o666)
        monkeypatch.setenv("LLM_CUSTOM_PROVIDERS", str(path))
        from modules.llm.provider_spec import reset_provider_registry_cache, user_declared_specs
        reset_provider_registry_cache()
        try:
            assert user_declared_specs() == ()
        finally:
            reset_provider_registry_cache()

    def test_builtin_override_cannot_move_transport(self, tmp_path, monkeypatch):
        path = tmp_path / "providers.yaml"
        path.write_text("providers:\n  anthropic: {transport: chat_completions}\n")
        monkeypatch.setenv("LLM_CUSTOM_PROVIDERS", str(path))
        from modules.llm.provider_spec import Transport, get_spec, reset_provider_registry_cache
        reset_provider_registry_cache()
        try:
            assert get_spec("anthropic").transport is Transport.ANTHROPIC_MESSAGES
        finally:
            reset_provider_registry_cache()

    def test_registry_off_ignores_user_file(self, user_providers, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER_REGISTRY", "false")
        from modules.llm.provider_spec import get_spec, reset_provider_registry_cache
        reset_provider_registry_cache()
        assert get_spec("ollama") is None

    def test_builtin_override_merges_in_place(self, tmp_path, monkeypatch):
        path = tmp_path / "providers.yaml"
        path.write_text(
            "providers:\n  openai:\n    base_url: http://gw.corp.internal/v1\n"
        )
        monkeypatch.setenv("LLM_CUSTOM_PROVIDERS", str(path))
        from modules.llm.provider_spec import get_specs, get_spec, reset_provider_registry_cache
        reset_provider_registry_cache()
        try:
            spec = get_spec("openai")
            assert spec.base_url == "http://gw.corp.internal/v1"
            assert spec.client_class_name == "OpenAIClient"  # kept from builtin
            assert spec.builtin is True
            names = [s.name for s in get_specs()]
            assert names.index("openai") == 2  # canonical position kept
        finally:
            reset_provider_registry_cache()


class TestDerivedSeams:
    def test_profiles_include_user_providers(self, user_providers):
        from modules.llm.profiles import PROFILES, get_profile
        assert "ollama" in PROFILES
        prof = get_profile("zai-coding")
        assert prof.env_key == "ZAI_API_KEY"
        assert prof.auth_type == "api_key"

    def test_usable_oracle_sees_keyed_user_provider(self, user_providers):
        from modules.llm.profiles import usable_providers_with_keys
        env = {"ZAI_API_KEY": "zk-0123456789abcdef0123456789"}
        assert usable_providers_with_keys(env) == ["ollama", "zai-coding"]
        # keyless-by-design (auth_type: none) is usable with no env at all — a
        # fresh box with only an Ollama row must pass every no-key gate
        # (UX assessment 2026-08-07, B2). Keyed builtins still outrank it in
        # canonical order; among user rows, file order decides.
        assert usable_providers_with_keys({}) == ["ollama"]

    def test_keyless_provider_is_initializable_without_env(self, user_providers):
        from modules.llm.profiles import (
            initializable_providers_with_keys,
            providers_with_keys,
        )
        assert initializable_providers_with_keys({}) == ["ollama"]
        # the DISPLAY oracle keeps env-key semantics: nothing to display for a
        # keyless row in a "which keys are present" listing
        assert providers_with_keys({}) == []

    def test_keyless_provider_quiets_no_key_nag(self, user_providers):
        from cli.keys import should_warn_no_key
        assert should_warn_no_key({}) is False

    def test_default_model_env_override_hyphenated_provider(self, user_providers, monkeypatch):
        """Q11: hyphenated provider names map to underscores — POSIX env names
        can't contain '-', so `zai-coding` reads POLYROB_ZAI_CODING_MODEL."""
        monkeypatch.setenv("POLYROB_ZAI_CODING_MODEL", "glm-4.6")
        from modules.llm.llm_client_registry import get_default_model
        assert get_default_model("zai-coding") == "glm-4.6"

    def test_no_key_message_names_declared_providers(self, user_providers):
        """The canonical no-key message derives from the spec registry: a
        declared row's env key is listed, so the message never denies the
        user's own configuration (assessment surface M)."""
        from modules.llm.profiles import no_key_message
        msg = no_key_message()
        assert "ZAI_API_KEY" in msg
        assert "providers.yaml" in msg

    def test_keyless_provider_auto_resolves(self, user_providers):
        from core.runtime_config import resolve_runtime_config
        # rung 4 on a keyless box: the auth-none row is the first (only)
        # usable provider — no more gemini last-resort on a working Ollama box
        assert resolve_runtime_config(None, None, env={}) == ("ollama", None)
        # name-based path (callers passing available_keys) agrees
        assert resolve_runtime_config(None, None, env={}, available_keys=set()) == ("ollama", None)
        # a keyed builtin still wins (canonical order puts builtins first)
        prov, _ = resolve_runtime_config(
            None, None, env={"ANTHROPIC_API_KEY": "sk-ant-" + "a" * 30}
        )
        assert prov == "anthropic"

    def test_provider_config_entry_uses_generic_client(self, user_providers):
        from modules.llm.model_registry import PROVIDER_CONFIG
        assert PROVIDER_CONFIG["ollama"].client_class_name == "OpenAICompatClient"
        assert PROVIDER_CONFIG["zai-coding"].client_class_name == "AnthropicCompatClient"
        assert PROVIDER_CONFIG["ollama"].fallback_eligible is False
        # builtins untouched
        assert PROVIDER_CONFIG["openai"].client_class_name == "OpenAIClient"

    def test_available_models_union(self, user_providers):
        from modules.llm.llm_client_registry import AVAILABLE_MODELS
        assert AVAILABLE_MODELS["ollama"] == ["qwen3-coder:30b", "llama3.3:70b"]
        assert "glm-5" in AVAILABLE_MODELS["zai-coding"]
        assert AVAILABLE_MODELS["openai"]  # registry-backed lists intact

    def test_get_default_model_from_spec(self, user_providers):
        from modules.llm.llm_client_registry import get_default_model
        assert get_default_model("ollama") == "qwen3-coder:30b"
        # env override still wins
        import os
        os.environ["POLYROB_OLLAMA_MODEL"] = "llama3.3:70b"
        try:
            assert get_default_model("ollama") == "llama3.3:70b"
        finally:
            del os.environ["POLYROB_OLLAMA_MODEL"]

    def test_model_map_routes_user_provider(self, user_providers):
        from api.openai_compat.model_map import map_model
        # slug head
        assert map_model("ollama/qwen3-coder:30b") == ("ollama", "qwen3-coder:30b")
        # registry ownership (declared model, exact)
        assert map_model("llama3.3:70b") == ("ollama", "llama3.3:70b")
        # declared prefix
        assert map_model("glm-9-preview") == ("zai-coding", "glm-9-preview")

    def test_schema_generator_by_transport(self, user_providers):
        from tools.controller.registry.schema_generators import (
            AnthropicSchemaGenerator,
            OpenAISchemaGenerator,
            get_schema_generator,
        )
        assert isinstance(get_schema_generator("ollama"), OpenAISchemaGenerator)
        assert isinstance(get_schema_generator("zai-coding"), AnthropicSchemaGenerator)

    def test_registry_supports_native_tools(self, user_providers):
        from tools.controller.registry.service import Registry
        reg = Registry.__new__(Registry)  # method reads no instance state
        assert reg.supports_native_tools("ollama") is True
        assert reg.supports_native_tools("definitely-unknown") is False

    def test_extra_llm_config_blocks(self, user_providers, monkeypatch):
        from modules.llm.profiles import extra_llm_config_blocks
        monkeypatch.setenv("ZAI_API_KEY", "zk-0123456789abcdef0123456789")
        blocks = extra_llm_config_blocks()
        # auth_type none → the sentinel, so LLMManager's api_key bootstrap
        # gates treat the provider as configured (024 live-path fix).
        assert blocks["ollama"]["api_key"] == "not-needed"
        assert blocks["ollama"]["base_url"] == "http://127.0.0.1:11434/v1"
        assert blocks["zai-coding"]["api_key"] == "zk-0123456789abcdef0123456789"
        # no builtin blocks when nothing redirected
        assert "openai" not in blocks

    def test_fallback_hierarchy_unchanged_by_default(self, user_providers):
        from modules.llm.llm_manager import LLMManager
        assert LLMManager._build_fallback_hierarchy() == [
            ("openai_client", "gpt-5"),
            ("anthropic_client", "claude-sonnet-4-5"),
            ("openrouter_client", "z-ai/glm-5.2"),
            ("gemini_client", "gemini-2.5-flash"),
        ]

    def test_factory_dispatches_generic_transport(self, user_providers):
        from modules.llm.adapters import AnthropicAdapter, OpenRouterAdapter
        from modules.llm.llm_factory import create_chat_model

        class _StubClient:
            model_type = None
        got = create_chat_model("ollama", "qwen3-coder:30b", 0.5, _StubClient())
        assert isinstance(got, OpenRouterAdapter)
        got = create_chat_model("zai-coding", "glm-5", 0.5, _StubClient())
        assert isinstance(got, AnthropicAdapter)


class TestLoadReport:
    """user_providers_report() — the queryable load state doctor renders (Q7)."""

    def _load(self, tmp_path, monkeypatch, yaml_text):
        path = tmp_path / "providers.yaml"
        path.write_text(yaml_text)
        monkeypatch.setenv("LLM_CUSTOM_PROVIDERS", str(path))
        monkeypatch.setenv("LLM_PROVIDER_REGISTRY", "true")
        from modules.llm.provider_spec import reset_provider_registry_cache
        reset_provider_registry_cache()
        return path

    def test_report_counts_loaded_and_rejected(self, tmp_path, monkeypatch):
        path = self._load(tmp_path, monkeypatch, textwrap.dedent("""\
            providers:
              good:
                base_url: http://127.0.0.1:9999/v1
                transport: chat_completions
                models: [m1]
              badrow:
                base_url: http://127.0.0.1:9999/v1
                transport: teleport
        """))
        from modules.llm.provider_spec import (
            reset_provider_registry_cache,
            user_providers_report,
        )
        try:
            rep = user_providers_report()
            assert rep["path"] == str(path)
            assert rep["loaded"] == ["good"]
            assert len(rep["rejected"]) == 1
            name, reason = rep["rejected"][0]
            assert name == "badrow"
            assert "transport" in reason
        finally:
            reset_provider_registry_cache()

    def test_report_empty_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_CUSTOM_PROVIDERS", str(tmp_path / "absent.yaml"))
        monkeypatch.setenv("LLM_PROVIDER_REGISTRY", "true")
        from modules.llm.provider_spec import (
            reset_provider_registry_cache,
            user_providers_report,
        )
        reset_provider_registry_cache()
        try:
            assert user_providers_report() == {}
        finally:
            reset_provider_registry_cache()

    def test_row_without_base_url_rejected(self, tmp_path, monkeypatch):
        """Q14: a new provider with neither base_url nor base_url_env would ride
        the generic client's DEFAULT endpoint (silent traffic misdirection) —
        reject the row instead."""
        self._load(tmp_path, monkeypatch, textwrap.dedent("""\
            providers:
              nourl:
                transport: chat_completions
                models: [m1]
        """))
        from modules.llm.provider_spec import (
            get_spec,
            reset_provider_registry_cache,
            user_providers_report,
        )
        try:
            assert get_spec("nourl") is None
            rejected = dict(user_providers_report()["rejected"])
            assert "base_url" in rejected["nourl"]
        finally:
            reset_provider_registry_cache()


class TestGenericClients:
    class _Cfg:
        """Minimal BotConfig stand-in: get_llm_config + the BaseComponent .get."""
        def __init__(self, blocks):
            self._blocks = blocks

        def get_llm_config(self):
            return dict(self._blocks)

        def get(self, key, default=None):
            return default

    def test_openai_compat_client_ollama(self, user_providers):
        from modules.llm.compat_clients import OpenAICompatClient
        client = OpenAICompatClient(
            config=self._Cfg({"ollama": {"api_key": None}}), name="ollama_client"
        )
        assert client.api_key == "not-needed"  # auth_type none sentinel
        assert client.model_type == "qwen3-coder:30b"
        assert client._profile_base_url() == "http://127.0.0.1:11434/v1"
        client._validate_llm_config()  # no key required — must not raise

    def test_anthropic_compat_client_zai(self, user_providers, monkeypatch):
        monkeypatch.setenv("ZAI_API_KEY", "zk-0123456789abcdef0123456789")
        from modules.llm.compat_clients import AnthropicCompatClient
        client = AnthropicCompatClient(
            config=self._Cfg({"zai-coding": {"api_key": None}}), name="zai-coding_client"
        )
        assert client.api_key == "zk-0123456789abcdef0123456789"
        assert client.model_type == "glm-5"
        assert client._profile_base_url() == "https://api.z.ai/api/anthropic"
        client._validate_llm_config()

    @pytest.mark.asyncio
    async def test_anthropic_compat_error_labels_name_declared_provider(
        self, user_providers, monkeypatch
    ):
        """Inherited failure messages must name the DECLARED provider, never
        'Anthropic' — a z.ai user has no Anthropic account to debug and no
        ANTHROPIC_API_KEY to check (UX assessment 2026-08-07, gap 4)."""
        monkeypatch.delenv("ZAI_API_KEY", raising=False)
        from core.exceptions import ServiceError
        from modules.llm.compat_clients import AnthropicCompatClient
        client = AnthropicCompatClient(
            config=self._Cfg({"zai-coding": {"api_key": None}}), name="zai-coding_client"
        )
        with pytest.raises(ServiceError) as ei:
            await client._initialize()
        msg = str(ei.value)
        assert "zai-coding" in msg
        assert "Anthropic" not in msg
        # the seam every inherited message site renders through
        assert client._PROVIDER_LABEL == client._spec.display_name

    def test_anthropic_compat_requires_key(self, user_providers):
        from core.exceptions import ServiceError
        from modules.llm.compat_clients import AnthropicCompatClient
        client = AnthropicCompatClient(
            config=self._Cfg({"zai-coding": {"api_key": None}}), name="zai-coding_client"
        )
        with pytest.raises(ServiceError, match="ZAI_API_KEY"):
            client._validate_llm_config()

    def test_create_llm_client_builds_generic(self, user_providers):
        from modules.llm.llm_client_registry import create_llm_client
        from modules.llm.compat_clients import OpenAICompatClient
        client = create_llm_client("ollama", self._Cfg({"ollama": {"api_key": None}}))
        assert isinstance(client, OpenAICompatClient)
        assert client.model_type == "qwen3-coder:30b"


class TestManagerLivePath:
    """The full LLMManager.get_chat_model path for user-declared providers —
    the seam the direct-construction tests above cannot cover (024 review C1:
    the class-name type guard and the api_key bootstrap gates both live here)."""

    class _Cfg:
        """BotConfig stand-in whose llm config comes from the REAL registry seam."""

        def get_llm_config(self):
            from modules.llm.profiles import extra_llm_config_blocks
            cfg = {}
            for name, block in extra_llm_config_blocks().items():
                cfg.setdefault(name, {}).update(block)
            return cfg

        def get(self, key, default=None):
            return default

    @pytest.mark.asyncio
    async def test_get_chat_model_serves_user_providers(self, user_providers, monkeypatch):
        monkeypatch.setenv("ZAI_API_KEY", "zk-0123456789abcdef0123456789")
        from modules.llm.adapters import AnthropicAdapter, OpenRouterAdapter
        from modules.llm.llm_client import LLMClient
        from modules.llm.llm_manager import LLMManager

        # Offline: skip the live connection probe, keep everything else real.
        monkeypatch.setattr(LLMClient, "_skip_validate", True)

        class _NullContainer:
            def has_service(self, name):
                return False

            def register_service(self, name, svc, is_optional=False):
                pass

            def get_service(self, name):
                return None

        null = _NullContainer()
        # BaseComponent lazily creates the DependencyContainer singleton, which
        # demands full config on first use — stub it for both manager and clients.
        monkeypatch.setattr(
            "core.container.DependencyContainer.get_instance",
            classmethod(lambda cls, config=None: null),
        )

        mgr = LLMManager("llm_manager", self._Cfg())
        mgr._container = null
        mgr._initialized = True  # exercise get_client → _try_initialize_client directly
        # ollama (auth_type none): the api_key sentinel must pass the bootstrap
        # gates and the spec-aware type guard must accept OpenAICompatClient.
        adapter = await mgr.get_chat_model("ollama", "qwen3-coder:30b", temperature=0.2)
        assert isinstance(adapter, OpenRouterAdapter)
        # zai-coding (anthropic_messages + env key) → AnthropicAdapter.
        adapter2 = await mgr.get_chat_model("zai-coding", "glm-5", temperature=0.2)
        assert isinstance(adapter2, AnthropicAdapter)
