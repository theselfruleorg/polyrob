"""B-T3 — Anthropic client sources base_url from the ProviderProfile.

Only OpenRouter read ProviderProfile.base_url; the Anthropic client built
``AsyncAnthropic(api_key=...)`` with no base_url, ignoring the profile. B-T3 mirrors
the OpenRouter pattern: read the profile's base_url (None => SDK default).

Task 2.4 update: _profile_base_url is now an instance method (delegating to the
base LLMClient._resolve_profile_base_url helper) rather than a classmethod.
"""
import asyncio
import logging
import anthropic

from modules.llm.anthropic_client import AnthropicClient


def _make_bare_client():
    """Create a minimal AnthropicClient instance without going through __init__."""
    client = AnthropicClient.__new__(AnthropicClient)
    client.api_key = "sk-test"
    client.logger = logging.getLogger("test")
    return client


def test_profile_base_url_reads_anthropic_profile():
    from modules.llm.profiles import get_profile

    expected = get_profile("anthropic").base_url
    client = _make_bare_client()
    assert client._profile_base_url() == expected


def test_setup_client_passes_base_url(monkeypatch):
    captured = {}

    class _FakeAsyncAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _FakeAsyncAnthropic)
    monkeypatch.setattr(
        AnthropicClient, "_profile_base_url",
        lambda self: "https://proxy.example/v1",
    )

    client = _make_bare_client()
    asyncio.run(client._setup_client())

    assert captured.get("api_key") == "sk-test"
    assert captured.get("base_url") == "https://proxy.example/v1"


def test_setup_client_omits_base_url_when_profile_none(monkeypatch):
    captured = {}

    class _FakeAsyncAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _FakeAsyncAnthropic)
    monkeypatch.setattr(
        AnthropicClient, "_profile_base_url",
        lambda self: None,
    )

    client = _make_bare_client()
    asyncio.run(client._setup_client())

    assert "base_url" not in captured  # SDK default
