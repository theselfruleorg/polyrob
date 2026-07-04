"""WS-2.3: provider-name single source of truth.

Provider STRINGS (not the ``ModelProvider`` enum) thread through the agent stack:
``Agent.provider_name``, the streaming whitelist, the schema-generator registry,
native-tools reconciliation. They had drifted across ≥4 hand-rolled maps — the
worst being ``GOOGLE`` (enum value ``"google"``) vs its canonical agent-facing
string ``"gemini"``, a mismatch that silently disabled Gemini streaming.

These tests pin every consumer to the one canonical map so the drift can't recur.
"""

from __future__ import annotations

from modules.llm.model_registry import (
    CANONICAL_PROVIDER_NAMES,
    PROVIDER_CANONICAL_NAMES,
    STREAMING_PROVIDER_NAMES,
    ModelProvider,
    canonical_provider_name,
)


def test_google_canonical_name_is_gemini_not_google():
    # The crux of the whole bug class.
    assert canonical_provider_name(ModelProvider.GOOGLE) == "gemini"
    assert "google" not in CANONICAL_PROVIDER_NAMES
    assert "gemini" in CANONICAL_PROVIDER_NAMES


def test_every_enum_member_has_a_canonical_name():
    # A new ModelProvider must be added to the map (this fails loudly otherwise).
    for member in ModelProvider:
        assert member in PROVIDER_CANONICAL_NAMES
        assert canonical_provider_name(member) != "generic"


def test_unknown_provider_falls_back_to_default():
    # Defensive: a None/garbage provider yields the supplied default.
    assert canonical_provider_name(None, default="openai") == "openai"  # type: ignore[arg-type]


def test_streaming_set_is_canonical_minus_custom():
    assert STREAMING_PROVIDER_NAMES == CANONICAL_PROVIDER_NAMES - {"custom"}


def test_schema_generators_cover_every_streaming_provider():
    # Every canonical real provider must resolve to a concrete schema generator —
    # a missing key would silently degrade native tool-calling for that provider.
    from tools.controller.registry.schema_generators import get_schema_generator

    for name in STREAMING_PROVIDER_NAMES:
        gen = get_schema_generator(name)
        assert gen is not None


def test_detect_llm_provider_uses_canonical_map():
    # detect_llm_provider must agree with the canonical map for a known model.
    from agents.task.utils import detect_llm_provider
    from modules.llm.model_registry import get_registry

    registry = get_registry()
    # Find one model per provider and assert detection matches the canonical name.
    seen: set = set()
    for model_name, cfg in registry._models.items():
        prov = cfg.provider
        if prov in seen:
            continue
        seen.add(prov)
        expected = canonical_provider_name(prov, default="generic")
        if expected == "generic":
            continue
        assert detect_llm_provider(None, model_name) == expected
    # Sanity: we actually exercised the canonical providers, not an empty registry.
    assert "gemini" in {canonical_provider_name(p) for p in seen}
