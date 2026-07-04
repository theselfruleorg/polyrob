"""P8 — openrouter client sources its transport base_url from ProviderProfile.

Task 2.4 update: _profile_base_url is now an instance method (delegating to the
base LLMClient._resolve_profile_base_url helper) rather than a classmethod.
"""
import logging


def _make_bare_openrouter():
    """Create a minimal OpenRouterClient instance without going through __init__."""
    from modules.llm.openrouter_client import OpenRouterClient
    client = OpenRouterClient.__new__(OpenRouterClient)
    client.logger = logging.getLogger("test")
    return client


def test_openrouter_base_url_comes_from_profile():
    from modules.llm.profiles import get_profile

    client = _make_bare_openrouter()
    expected = get_profile("openrouter").base_url
    assert client._profile_base_url() == expected
    assert "openrouter.ai" in client._profile_base_url()


def test_falls_back_to_constant_if_profile_missing(monkeypatch):
    from modules.llm.openrouter_client import OpenRouterClient

    monkeypatch.setattr("modules.llm.profiles.get_profile", lambda name: None)
    client = _make_bare_openrouter()
    assert client._profile_base_url() == OpenRouterClient.OPENROUTER_BASE_URL
