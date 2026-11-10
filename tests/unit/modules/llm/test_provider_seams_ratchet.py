"""A built-in provider that misses a seam fails here (proposal 106, cause 3, Appendix B).

``ProviderSpec`` wins: the client class, the schema generator, the native-tools answer, the
config block and the model list derive from the row. ``missing_seams`` reads each result the
way a request reads it, so a seam someone forgot shows up by name.
"""
import importlib.util
import inspect
from types import SimpleNamespace

import pytest

import core.config
from modules.llm import llm_factory
from modules.llm.adapters import AnthropicAdapter, GeminiAdapter, OpenAIAdapter, OpenRouterAdapter
from modules.llm.llm_client_registry import _CLIENT_MODULES, AVAILABLE_MODELS, DEFAULT_MODELS
from modules.llm.model_types import CANONICAL_PROVIDER_NAMES
from modules.llm.profiles import extra_llm_config_blocks
from modules.llm.provider_spec import (
    BUILTIN_SPECS,
    ProviderSpec,
    Transport,
    generic_client_class_name,
    needs_synthetic_config_block,
)
from tools.controller.registry.schema_generators import (
    AnthropicSchemaGenerator,
    GeminiSchemaGenerator,
    JSONFallbackSchemaGenerator,
    OpenAISchemaGenerator,
    get_schema_generator,
    supports_native_tools,
)

#: The aux decision: a provider's spec has an ``aux_model``, an ``aux_family_map``, or it is
#: named here as aux = main on purpose. A new built-in must take one of the three.
AUX_IS_MAIN = frozenset({
    "nvidia", "ollama-cloud", "cerebras", "moonshot", "moonshot-cn", "kimi-coding", "minimax",
    "minimax-cn", "xai", "dashscope", "alibaba-coding", "stepfun", "ai-gateway", "opencode-zen",
    "opencode-go", "kilocode", "huggingface", "xiaomi", "tencent-tokenhub", "copilot",
    "anthropic-oauth", "openai-codex", "github-copilot", "xai-oauth", "qwen-oauth", "minimax-oauth",
})


def _expected_adapter(spec):
    return {"OpenAIClient": OpenAIAdapter, "GeminiClient": GeminiAdapter}.get(
        spec.client_class_name,
        AnthropicAdapter if spec.transport is Transport.ANTHROPIC_MESSAGES else OpenRouterAdapter)


def _expected_generator(spec):
    if spec.client_class_name == "GeminiClient":
        return GeminiSchemaGenerator
    if spec.transport is Transport.ANTHROPIC_MESSAGES:
        return AnthropicSchemaGenerator
    return OpenAISchemaGenerator


def _adapter_seam(spec, default_model):
    try:
        built = llm_factory.create_chat_model(
            spec.name, default_model or "m", 0.0, SimpleNamespace(model_type="m"))
    except ImportError:
        return True  # the SDK extra is not installed here
    except ValueError:
        return False
    return type(built) is _expected_adapter(spec)


def _config_seam(spec):
    if needs_synthetic_config_block(spec):
        env = {spec.env_key: "k"} if spec.env_key else {}
        return "api_key" in extra_llm_config_blocks(env).get(spec.name, {})
    return f'"{spec.name}": {{' in inspect.getsource(core.config)


def missing_seams(spec):
    """The Appendix B seams *spec* misses, by name."""
    missing = []
    client = spec.client_class_name or generic_client_class_name(spec.transport)
    row = _CLIENT_MODULES.get(client)
    if row is None or importlib.util.find_spec(row[0]) is None:
        missing.append("client module")
    default_model = spec.default_model or DEFAULT_MODELS.get(spec.name)
    if not default_model:
        missing.append("default model")
    if not _adapter_seam(spec, default_model):
        missing.append("adapter")
    if not _config_seam(spec):
        missing.append("config block")
    if spec.client_class_name is not None and spec.name not in CANONICAL_PROVIDER_NAMES:
        missing.append("canonical name")
    if not AVAILABLE_MODELS.get(spec.name):
        missing.append("enum map or model list")
    generator = get_schema_generator(spec.name)
    if not isinstance(generator, _expected_generator(spec)):
        missing.append("schema generator")
    if not (spec.aux_model or spec.aux_family_map or spec.name in AUX_IS_MAIN):
        missing.append("aux decision")
    return missing


@pytest.fixture(autouse=True)
def _rig(monkeypatch):
    monkeypatch.setenv("LLM_CUSTOM_PROVIDERS", "")
    monkeypatch.setattr("modules.llm.billing_guard.require_billable_model", lambda *a, **k: None)
    monkeypatch.setattr("core.lazy_deps.ensure_provider", lambda *a, **k: None)


@pytest.mark.parametrize("spec", BUILTIN_SPECS, ids=lambda s: s.name)
def test_each_builtin_provider_has_every_seam(spec):
    assert missing_seams(spec) == []


def test_a_provider_that_misses_every_seam_is_named():
    ghost = ProviderSpec(name="ghost-prov", display_name="Ghost", env_key="GHOST_KEY",
                         client_class_name="GhostClient", builtin=True)
    assert missing_seams(ghost) == [
        "client module", "default model", "adapter", "config block", "canonical name",
        "enum map or model list", "schema generator", "aux decision",
    ]


def test_each_canonical_provider_has_a_spec():
    names = {s.name for s in BUILTIN_SPECS}
    assert CANONICAL_PROVIDER_NAMES - {"custom"} <= names


def test_an_aux_entry_names_a_real_provider():
    names = {s.name for s in BUILTIN_SPECS}
    assert AUX_IS_MAIN <= names


@pytest.mark.parametrize("spec", BUILTIN_SPECS, ids=lambda s: s.name)
def test_the_generator_and_the_native_check_agree(spec):
    assert supports_native_tools(spec.name) is spec.supports_native_tools


def _user_row(monkeypatch, **fields):
    row = ProviderSpec(name="groq", display_name="Groq", **fields)
    monkeypatch.setattr("modules.llm.provider_spec.get_spec",
                        lambda name: row if name == "groq" else None)


def test_a_user_row_named_like_an_old_table_row_reads_its_spec(monkeypatch):
    _user_row(monkeypatch)
    assert isinstance(get_schema_generator("groq"), OpenAISchemaGenerator)
    assert supports_native_tools("groq") is True


def test_a_spec_with_no_native_tools_gets_the_fallback_and_the_check_agrees(monkeypatch):
    _user_row(monkeypatch, supports_native_tools=False)
    assert isinstance(get_schema_generator("groq"), JSONFallbackSchemaGenerator)
    assert supports_native_tools("groq") is False


def test_the_responses_transport_gets_the_openai_shape(monkeypatch):
    _user_row(monkeypatch, transport=Transport.RESPONSES)
    assert isinstance(get_schema_generator("groq"), OpenAISchemaGenerator)


def test_a_name_with_no_spec_reads_the_builtin_it_holds():
    assert isinstance(get_schema_generator("openai-gpt4"), OpenAISchemaGenerator)
    assert isinstance(get_schema_generator("google"), GeminiSchemaGenerator)
    assert isinstance(get_schema_generator("no-such-provider-xyz"), JSONFallbackSchemaGenerator)
