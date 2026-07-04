"""P8 — declarative ProviderProfile layer (additive; no client rewiring)."""
import pytest

from modules.llm.profiles import (
    ProviderProfile, get_profile, all_profiles, PROFILES,
)
from modules.llm.llm_client_registry import DEFAULT_MODELS


def test_known_providers_have_profiles():
    for name in ("anthropic", "openai", "gemini", "openrouter"):
        p = get_profile(name)
        assert isinstance(p, ProviderProfile)
        assert p.name == name
        assert p.env_key  # has an API-key env var
        assert p.display_name


def test_default_model_is_sourced_from_registry_single_source():
    # Profiles must not duplicate the default-model policy; they read the registry.
    for name, expected in DEFAULT_MODELS.items():
        p = get_profile(name)
        if p is not None:
            assert p.default_model == expected


def test_native_tools_flags():
    assert get_profile("anthropic").supports_native_tools is True
    assert get_profile("openai").supports_native_tools is True
    assert get_profile("gemini").supports_native_tools is True


def test_unknown_provider_returns_none():
    assert get_profile("nonesuch") is None


def test_all_profiles_nonempty_and_unique_names():
    profs = all_profiles()
    names = [p.name for p in profs]
    assert len(names) == len(set(names))
    assert set(names) == set(PROFILES.keys())


def test_openrouter_has_custom_base_url():
    assert "openrouter.ai" in get_profile("openrouter").base_url


def test_profile_is_frozen():
    import dataclasses
    p = get_profile("anthropic")
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.name = "x"
