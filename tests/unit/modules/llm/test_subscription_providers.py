"""Shipped subscription/flat-rate provider rows (proposal 024, T0).

Two things landed together and are pinned here:

1. Built-in ``ProviderSpec`` rows for plans that issue an API KEY (no OAuth) —
   Ollama Cloud, the z.ai GLM Coding Plan, Cerebras — so a subscriber no longer
   has to hand-author a ``providers.yaml`` row to use the plan they pay for.
2. ``ProviderSpec.subscription`` finally MEANS something: before T0 the field
   was declared and read by nothing, so a flat-rate plan was billed per token
   in ``usage_records`` as if it were metered.

⚠ These rows are declared from vendor documentation and are NOT live-verified
(024 §8 requires a real tool-EXECUTING run per rail). Nothing here claims
otherwise — these tests pin wiring and accounting, not reachability.
"""
import pytest


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    """Built-ins only: no stray providers.yaml, registry on."""
    monkeypatch.setenv("LLM_PROVIDER_REGISTRY", "true")
    monkeypatch.setenv("LLM_CUSTOM_PROVIDERS", "")
    from modules.llm.provider_spec import reset_provider_registry_cache
    reset_provider_registry_cache()
    yield
    reset_provider_registry_cache()


SUBSCRIPTION_ROWS = ("ollama-cloud", "zai-coding", "cerebras")


# ---------------------------------------------------------------------------
# The rows themselves
# ---------------------------------------------------------------------------
class TestShippedRows:
    def test_ollama_cloud_is_distinct_from_a_local_ollama(self):
        """Same vendor, two products — the hosted one needs a key.

        A local Ollama stays a keyless ``ollama`` providers.yaml row pointed at
        loopback. Collapsing them would either demand a key for a loopback
        server or send a paid key to 127.0.0.1.
        """
        from modules.llm.provider_spec import AuthType, Transport, get_spec
        spec = get_spec("ollama-cloud")
        assert spec is not None
        assert spec.base_url == "https://ollama.com/v1"
        assert spec.env_key == "OLLAMA_API_KEY"
        assert spec.auth_type is AuthType.API_KEY      # NOT keyless
        assert spec.transport is Transport.CHAT_COMPLETIONS
        assert get_spec("ollama") is None               # local row is not shipped

    def test_zai_coding_speaks_anthropic_with_bearer(self):
        from modules.llm.provider_spec import Transport, get_spec
        spec = get_spec("zai-coding")
        assert spec.transport is Transport.ANTHROPIC_MESSAGES
        # z.ai authenticates Authorization: Bearer, not x-api-key — getting this
        # wrong is a 401 on every call.
        assert spec.bearer_auth is True
        assert spec.env_key == "ZAI_API_KEY"

    def test_rows_resolve_by_alias(self):
        from modules.llm.provider_spec import get_spec
        assert get_spec("ollama_cloud").name == "ollama-cloud"
        assert get_spec("zai-code").name == "zai-coding"
        # `zai` was zai-coding's alias until the standard pay-as-you-go Z.AI
        # API shipped as its own row. An alias must never win over a real
        # provider of that name — the two are different endpoints, and the
        # coding-plan row speaks Anthropic while the standard API speaks
        # OpenAI, so the mixup went out as an unusable tool schema.
        assert get_spec("zai").name == "zai"

    def test_env_keys_are_api_key_shaped(self):
        """The same shape providers.yaml rows are validated against.

        A row whose ``env_key`` is not ``*_API_KEY`` is refused at load as a
        secret-exfiltration guard; shipped rows must satisfy the rule they are
        the reference implementation of.
        """
        from modules.llm.provider_spec import _ENV_KEY_RE, get_spec
        for name in SUBSCRIPTION_ROWS:
            assert _ENV_KEY_RE.match(get_spec(name).env_key), name

    def test_rows_cannot_hijack_an_existing_install(self):
        """Appended last, fallback-ineligible, no prefix routing.

        Every one of these serves open-weight ids (glm/kimi/gpt-oss) that other
        providers also serve, so a model_prefix would silently steal routing.
        """
        from modules.llm.profiles import PROFILES, providers_with_keys
        from modules.llm.provider_spec import get_spec
        assert list(PROFILES.keys())[:6] == [
            "openrouter", "anthropic", "openai", "gemini", "nvidia", "deepseek"
        ]
        for name in SUBSCRIPTION_ROWS:
            spec = get_spec(name)
            assert spec.fallback_eligible is False, name
            assert spec.model_prefixes == (), name
        # An install with only a legacy key resolves exactly as before.
        legacy = {"OPENROUTER_API_KEY": "sk-0123456789abcdef0123456789"}
        assert providers_with_keys(legacy) == ["openrouter"]

    def test_rows_are_inert_without_their_key(self):
        """Shipping a row must not change a box that has none of these plans."""
        from modules.llm.profiles import usable_providers_with_keys
        assert usable_providers_with_keys({}) == []

    def test_key_alone_makes_the_provider_usable(self):
        from modules.llm.profiles import usable_providers_with_keys
        env = {"OLLAMA_API_KEY": "ok-0123456789abcdef0123456789"}
        assert usable_providers_with_keys(env) == ["ollama-cloud"]


# ---------------------------------------------------------------------------
# Bootstrap wiring — the landmine that makes a row real vs decorative
# ---------------------------------------------------------------------------
class TestBootstrapWiring:
    def test_rows_get_a_synthesized_llm_config_block(self):
        """Without one, LLMManager skips the provider and it is silently dead.

        ``LLMManager._initialize`` gates each candidate on
        ``config_data.get('api_key')``. The six legacy built-ins get that from a
        literal block in ``BotConfig.get_llm_config`` keyed off a pydantic
        field; a T0 row has no such field, so ``extra_llm_config_blocks`` must
        synthesize the block or the row would look configured and never load.
        """
        from modules.llm.profiles import extra_llm_config_blocks
        env = {"OLLAMA_API_KEY": "ok-0123456789abcdef0123456789"}
        blocks = extra_llm_config_blocks(env)
        assert blocks["ollama-cloud"]["api_key"] == env["OLLAMA_API_KEY"]
        assert blocks["ollama-cloud"]["base_url"] == "https://ollama.com/v1"

    def test_legacy_builtins_keep_their_literal_block(self):
        """The synthesis must not start shadowing the six pydantic-backed keys."""
        from modules.llm.profiles import extra_llm_config_blocks
        blocks = extra_llm_config_blocks({"OPENAI_API_KEY": "sk-x" * 10})
        assert "openai" not in blocks and "anthropic" not in blocks

    def test_rows_route_to_a_generic_client(self):
        from modules.llm.model_registry import PROVIDER_CONFIG
        assert PROVIDER_CONFIG["ollama-cloud"].client_class_name == "OpenAICompatClient"
        assert PROVIDER_CONFIG["zai-coding"].client_class_name == "AnthropicCompatClient"
        assert PROVIDER_CONFIG["cerebras"].client_class_name == "OpenAICompatClient"

    def test_rows_carry_their_own_default_model(self):
        """A T0 row must never inherit the openai default (`gpt-5`).

        Asking an Ollama Cloud endpoint for `gpt-5` is a 404 that reads like a
        POLYROB bug.
        """
        from modules.llm.llm_client_registry import get_default_model
        for name in SUBSCRIPTION_ROWS:
            assert get_default_model(name) not in ("gpt-5", ""), name

    def test_schema_generator_routes_per_transport(self):
        """A SCHEMA_GENERATORS miss is THE silent failure mode for a new rail.

        It does not raise: the provider is simply shipped `tools=0`, the model
        answers in prose, and every other check looks healthy. Pin the routing
        so a future row cannot land unrouted.
        """
        from tools.controller.registry.schema_generators import (
            AnthropicSchemaGenerator,
            JSONFallbackSchemaGenerator,
            OpenAISchemaGenerator,
            get_schema_generator,
        )
        expected = {
            "ollama-cloud": OpenAISchemaGenerator,
            "cerebras": OpenAISchemaGenerator,
            "zai-coding": AnthropicSchemaGenerator,
        }
        for name, cls in expected.items():
            gen = get_schema_generator(name)
            assert isinstance(gen, cls), f"{name} routed to {type(gen).__name__}"
            assert not isinstance(gen, JSONFallbackSchemaGenerator), name

    def test_declared_models_are_listable(self):
        from modules.llm.llm_client_registry import AVAILABLE_MODELS
        assert "glm-5.2" in AVAILABLE_MODELS["ollama-cloud"]

    def test_base_url_env_override_is_honored(self, monkeypatch):
        from modules.llm.provider_spec import get_spec
        monkeypatch.setenv("OLLAMA_CLOUD_BASE_URL", "https://proxy.internal/v1")
        assert get_spec("ollama-cloud").resolved_base_url() == "https://proxy.internal/v1"


# ---------------------------------------------------------------------------
# `subscription` now has a consumer: flat-rate cost accounting
# ---------------------------------------------------------------------------
class TestFlatRateAccounting:
    def test_is_flat_rate_reads_the_spec(self):
        from modules.llm.provider_spec import is_flat_rate
        assert is_flat_rate("ollama-cloud") is True
        assert is_flat_rate("zai-coding") is True
        # Cerebras serves BOTH Code Pro/Max and pay-per-token on one endpoint —
        # defaulting to metered can only ever overstate cost.
        assert is_flat_rate("cerebras") is False
        assert is_flat_rate("openai") is False

    def test_is_flat_rate_fails_open_to_metered(self):
        """Unknown/blank must METER, never silently zero someone's real bill."""
        from modules.llm.provider_spec import is_flat_rate
        assert is_flat_rate(None) is False
        assert is_flat_rate("") is False
        assert is_flat_rate("no-such-provider") is False

    def test_flat_rate_provider_records_zero_marginal_cost(self):
        from modules.credits.pricing import compute_llm_cost
        usage = {"prompt_tokens": 500_000, "completion_tokens": 200_000}
        # Same model+usage: metered through a priced provider, free on the seat.
        assert compute_llm_cost("claude-sonnet-4-5", usage) > 0
        assert compute_llm_cost("claude-sonnet-4-5", usage, provider="zai-coding") == 0.0

    def test_omitting_provider_is_byte_identical(self):
        """Every pre-T0 caller passes no provider and must be unaffected."""
        from modules.credits.pricing import compute_llm_cost
        usage = {"prompt_tokens": 1000, "completion_tokens": 500, "cached_tokens": 200}
        assert (compute_llm_cost("claude-sonnet-4-5", usage)
                == compute_llm_cost("claude-sonnet-4-5", usage, provider=None)
                == compute_llm_cost("claude-sonnet-4-5", usage, provider="anthropic"))

    def test_user_can_declare_their_own_plan_flat_rate(self, tmp_path, monkeypatch):
        """A Cerebras Code subscriber overrides the shipped metered default."""
        path = tmp_path / "providers.yaml"
        path.write_text("providers:\n  cerebras:\n    subscription: true\n")
        path.chmod(0o600)
        monkeypatch.setenv("LLM_CUSTOM_PROVIDERS", str(path))
        from modules.llm.provider_spec import is_flat_rate, reset_provider_registry_cache
        reset_provider_registry_cache()
        assert is_flat_rate("cerebras") is True


# ---------------------------------------------------------------------------
# Onboarding: named, not prompted
# ---------------------------------------------------------------------------
def test_subscription_rows_are_not_prompted_in_init():
    """`polyrob init` Section 1/6 is position-sensitive; six prompts stay six."""
    from modules.llm.provider_spec import get_spec
    for name in SUBSCRIPTION_ROWS:
        assert get_spec(name).prompt_in_init is False, name


def test_restating_a_builtin_transport_is_not_a_conflict(tmp_path, monkeypatch, caplog):
    """Users hand-authored `zai-coding` before it shipped; their file is right.

    Refusing a transport CHANGE on a built-in is the real guard. Warning about a
    row that merely restates the built-in's own transport would fire on every
    load of a correct file.
    """
    path = tmp_path / "providers.yaml"
    path.write_text(
        "providers:\n"
        "  zai-coding:\n"
        "    base_url: https://api.z.ai/api/anthropic\n"
        "    env_key: ZAI_API_KEY\n"
        "    transport: anthropic_messages\n"
    )
    path.chmod(0o600)
    monkeypatch.setenv("LLM_CUSTOM_PROVIDERS", str(path))
    from modules.llm.provider_spec import get_spec, reset_provider_registry_cache
    reset_provider_registry_cache()
    with caplog.at_level("WARNING"):
        assert get_spec("zai-coding") is not None
    assert "transport' cannot be overridden" not in caplog.text


# ---------------------------------------------------------------------------
# reference-parity breadth (2026-08-12): every shipped row must be reachable and
# correctly routed. These are whole-table invariants, not per-row assertions —
# a new row that violates one is a silent misroute, not a visible failure.
# ---------------------------------------------------------------------------
class TestProviderTableInvariants:
    def test_no_alias_shadows_a_real_provider_name(self):
        """An alias claiming another row's NAME silently hijacks it.

        Shipping `zai` alongside `zai-coding` (which aliased `zai`) routed every
        `zai` lookup to the coding-plan row: an OpenAI-compatible endpoint got
        the Anthropic schema generator, so it would have shipped tools the
        provider cannot parse — visible only as the model ignoring its tools.
        """
        from modules.llm.provider_spec import get_specs
        specs = get_specs()
        names = {s.name for s in specs}
        offenders = [(s.name, a) for s in specs for a in s.aliases
                     if a in names - {s.name}]
        assert not offenders, f"alias shadows a real provider name: {offenders}"

    def test_exact_name_beats_an_alias_regardless_of_order(self):
        from modules.llm.provider_spec import get_spec
        assert get_spec("zai").name == "zai"
        assert get_spec("zai-coding").name == "zai-coding"

    def test_every_row_routes_to_a_real_schema_generator(self):
        """A JSON-fallback generator means tools go out in a shape the provider
        does not understand — the silent tools=0 failure, per provider."""
        from tools.controller.registry.schema_generators import (
            AnthropicSchemaGenerator,
            JSONFallbackSchemaGenerator,
            OpenAISchemaGenerator,
            get_schema_generator,
        )
        from modules.llm.provider_spec import Transport, get_specs
        expected = {
            Transport.CHAT_COMPLETIONS: OpenAISchemaGenerator,
            Transport.ANTHROPIC_MESSAGES: AnthropicSchemaGenerator,
            # Responses is an OpenAI-shaped tool schema (flattened by the
            # client at the wire boundary, not by a fourth generator).
            Transport.RESPONSES: OpenAISchemaGenerator,
        }
        for s in get_specs():
            if s.client_class_name is not None:
                continue                      # bespoke clients route by name
            if s.transport not in expected:
                # RESPONSES is declared (openai-codex) but has no generic client
                # yet, so it is absent from PROVIDER_CONFIG and cannot be routed.
                # Assert that state explicitly rather than silently skipping it.
                from modules.llm.provider_spec import generic_client_class_name
                assert generic_client_class_name(s.transport) is None, s.name
                continue
            gen = get_schema_generator(s.name)
            assert not isinstance(gen, JSONFallbackSchemaGenerator), s.name
            assert isinstance(gen, expected[s.transport]), (
                f"{s.name} declares {s.transport.value} but routes to "
                f"{type(gen).__name__}"
            )

    def test_every_default_model_is_one_the_row_declares(self):
        """A default the provider does not serve is a 404 that reads as our bug."""
        from modules.llm.llm_client_registry import get_default_model
        from modules.llm.provider_spec import get_specs
        for s in get_specs():
            if not s.models:
                continue
            assert get_default_model(s.name) in s.models, s.name

    def test_no_env_var_is_claimed_by_two_rows(self):
        """One variable claimed by two rows with different endpoints routes a
        key at whichever row wins the ordering — a 401 with no explanation."""
        from modules.llm.provider_spec import get_specs
        owner = {}
        clashes = []
        for s in get_specs():
            for var in s.env_key_chain():
                if var in owner:
                    clashes.append((var, owner[var], s.name))
                owner[var] = s.name
        assert not clashes, f"env var claimed by multiple providers: {clashes}"

    def test_new_rows_never_join_the_fallback_hierarchy(self):
        """Failover onto a paid seat (or off it) is never automatic."""
        from modules.llm.provider_spec import BUILTIN_SPECS
        legacy = {"openrouter", "anthropic", "openai", "gemini", "nvidia", "deepseek"}
        for s in BUILTIN_SPECS:
            if s.name not in legacy:
                assert s.fallback_eligible is False, s.name
                assert s.prompt_in_init is False, s.name


# ---------------------------------------------------------------------------
# Post-parity audit (2026-08-12). Each of these is a bug that shipped in the
# breadth wave and was caught by auditing the table as a whole.
# ---------------------------------------------------------------------------
class TestParityAuditRegressions:
    def test_a_row_with_no_client_is_never_offered_as_usable(self, monkeypatch):
        """`openai-codex` (transport RESPONSES) was reported USABLE, auto-
        selected by the resolver, and then died in create_llm_client with
        "Unknown LLM client type" — a hard crash where an honest refusal
        belonged.

        RESPONSES has a client now, so this pins the MECHANISM against the next
        transport declared before its client lands: initializable is DERIVED
        from "can anything build a client for this", never hand-set.
        """
        import modules.llm.provider_spec as ps
        # simulate a transport whose client has not landed yet
        monkeypatch.setitem(ps._GENERIC_CLIENT_FOR_TRANSPORT, ps.Transport.RESPONSES, None)
        ps.reset_provider_registry_cache()
        try:
            from modules.llm.profiles import usable_providers_with_credentials
            assert ps.get_spec("openai-codex").initializable is False
            assert "openai-codex" not in usable_providers_with_credentials(
                {"OPENAI_API_KEY": "sk-" + "x" * 30})
        finally:
            ps.reset_provider_registry_cache()

    def test_generic_developer_env_vars_are_never_claimed(self):
        """HF_TOKEN (huggingface-cli/datasets/transformers) and GH_TOKEN are set
        on a large share of dev machines for reasons unrelated to inference.
        Claiming either would silently route ALL of a box's inference through
        that provider the moment it was the only credential present."""
        from modules.llm.profiles import usable_providers_with_credentials
        from modules.llm.provider_spec import get_specs
        generic = {"HF_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "OPENAI_BASE_URL"}
        for s in get_specs():
            assert not (set(s.env_key_chain()) & generic), (s.name, s.env_key_chain())
        for var in ("HF_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
            assert usable_providers_with_credentials({var: "x" * 30}) == [], var

    def test_credential_status_reads_the_store_once_not_once_per_provider(
            self, monkeypatch, tmp_path):
        """AuthStore.get_provider re-reads the file under an flock every call.
        Across 30+ providers, one readiness check became 30+ locked reads — and
        three oracles plus every gate call it."""
        monkeypatch.setenv("POLYROB_AUTH_STORE", str(tmp_path / "auth.json"))
        monkeypatch.setenv("LLM_AUTH_STORE_ENABLED", "true")
        monkeypatch.setenv("POLYROB_LOCAL", "1")
        import core.llm_auth.store as store_mod
        from core.llm_auth.store import AuthStore
        store = AuthStore(str(tmp_path / "auth.json"))
        store.set_provider("anthropic-oauth", {"access_token": "tok"})
        reads = {"n": 0}
        real_load = store.load

        def counting_load():
            reads["n"] += 1
            return real_load()

        monkeypatch.setattr(store, "load", counting_load)
        monkeypatch.setattr(store_mod, "get_auth_store", lambda *a, **k: store)

        from modules.llm.profiles import credential_status
        rows = credential_status()
        assert reads["n"] == 1, f"{reads['n']} store reads for one sweep"
        # ...and the snapshot still resolves the stored credential
        assert rows["anthropic-oauth"].source == "oauth"

    def test_no_key_message_stays_short_but_names_user_declared_rows(
            self, tmp_path, monkeypatch):
        """It named all 27 variables (~950 chars) on a first run. Trimming it
        must NOT drop a row the user declared themselves — an override of a
        built-in inherits prompt_in_init=False, which silently removed exactly
        the variable that user needed named."""
        path = tmp_path / "providers.yaml"
        path.write_text(
            "providers:\n  mygateway:\n    base_url: https://gw.example.com/v1\n"
            "    env_key: MYGATEWAY_API_KEY\n    transport: chat_completions\n"
            "    models: [m1]\n"
        )
        path.chmod(0o600)
        monkeypatch.setenv("LLM_CUSTOM_PROVIDERS", str(path))
        from modules.llm.profiles import no_key_message
        from modules.llm.provider_spec import reset_provider_registry_cache
        reset_provider_registry_cache()
        msg = no_key_message()
        assert "MYGATEWAY_API_KEY" in msg          # user's own row is named
        assert "OPENROUTER_API_KEY" in msg         # onboarding six still named
        assert "XIAOMI_API_KEY" not in msg         # long tail is counted, not listed
        assert "more providers" in msg
        assert len(msg) < 900, f"{len(msg)} chars — the wall is back"

    def test_billing_never_borrows_another_vendors_price(self):
        """The registry's family fallback ends at a cross-vendor default, so
        "what does MiniMax-M2.7 cost" was answered with GPT-5.1's $2/$8 per M.
        Same-vendor proxies stay (claude-sonnet-4-6 -> claude-sonnet-4-5);
        cross-vendor records $0 and warns, because inventing a number is worse
        than admitting we have no price."""
        from modules.credits.pricing import compute_llm_cost
        usage = {"prompt_tokens": 100_000, "completion_tokens": 50_000}
        # cross-vendor guesses are refused
        for provider, model in [("minimax", "MiniMax-M2.7"),
                                ("stepfun", "step-3.5-flash"),
                                ("xiaomi", "mimo-v2.5-pro"),
                                ("tencent-tokenhub", "hy3")]:
            assert compute_llm_cost(model, usage, provider) == 0.0, provider
        # real prices are untouched
        assert compute_llm_cost("claude-sonnet-4-5", usage, "anthropic") > 0
        assert compute_llm_cost("gpt-5", usage, "openai") > 0
        assert compute_llm_cost("z-ai/glm-5.2", usage, "openrouter") > 0
        # and omitting the provider is byte-identical to before
        assert compute_llm_cost("MiniMax-M2.7", usage) > 0

    def test_ambiguous_model_ownership_prefers_a_provider_you_can_use(self, monkeypatch):
        """Several providers now serve the same open-weight id. Routing by
        declaration order sent a request to one the caller has no key for."""
        monkeypatch.setenv("GLM_API_KEY", "gk-0123456789abcdefghijklmn")
        for var in ("OLLAMA_API_KEY", "OPENROUTER_API_KEY", "ZAI_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        from api.openai_compat.model_map import _provider_owning
        from modules.llm.llm_client_registry import AVAILABLE_MODELS
        owners = [p for p, m in AVAILABLE_MODELS.items() if "glm-5.2" in m]
        assert len(owners) > 1, "expected an ambiguous id for this test to mean anything"
        assert _provider_owning("glm-5.2") == "zai"

    def test_a_connected_oauth_seat_reaches_the_client_layer(self, monkeypatch, tmp_path):
        """THE gap that made the whole credential store decorative.

        `polyrob auth add` succeeded, doctor reported the provider ready, and
        every call then died with "API key not provided" — because
        extra_llm_config_blocks synthesized api_key from ENVIRONMENT VARIABLES
        only, so a store-backed seat never reached client construction.
        """
        monkeypatch.setenv("POLYROB_AUTH_STORE", str(tmp_path / "auth.json"))
        monkeypatch.setenv("LLM_AUTH_STORE_ENABLED", "true")
        monkeypatch.setenv("POLYROB_LOCAL", "1")
        import core.llm_auth.store as store_mod
        from core.llm_auth.store import AuthStore
        store = AuthStore(str(tmp_path / "auth.json"))
        store.set_provider("anthropic-oauth",
                           {"access_token": "seat-token", "expires_at": 9e12})
        monkeypatch.setattr(store_mod, "get_auth_store", lambda *a, **k: store)

        from modules.llm.profiles import extra_llm_config_blocks
        block = extra_llm_config_blocks().get("anthropic-oauth") or {}
        assert block.get("api_key") == "seat-token"

        from core.config import BotConfig
        from modules.llm.llm_client_registry import create_llm_client
        client = create_llm_client("anthropic-oauth", BotConfig())
        assert client.api_key == "seat-token"

    def test_an_unservable_transport_refuses_with_a_reason(self, monkeypatch):
        """"Unknown LLM client type" sends the user hunting for a typo that
        isn't there. A declared provider whose transport has no client is a
        different, reachable failure and must say so."""
        import modules.llm.provider_spec as ps
        from core.config import BotConfig
        from modules.llm.llm_client_registry import create_llm_client

        with pytest.raises(ValueError, match="Unknown LLM client type"):
            create_llm_client("definitely-not-a-provider", BotConfig())

        monkeypatch.setitem(ps._GENERIC_CLIENT_FOR_TRANSPORT, ps.Transport.RESPONSES, None)
        ps.reset_provider_registry_cache()
        try:
            with pytest.raises(ValueError, match="cannot serve yet"):
                create_llm_client("openai-codex", BotConfig())
        finally:
            ps.reset_provider_registry_cache()
