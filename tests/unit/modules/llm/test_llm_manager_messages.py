"""LLMManager user-facing message builders (UX assessment 2026-08-07, Q13/Q15).

The provider-unavailable remedy must work for INSTALLED users — the old string
pointed at ``config/.env.development``, a repo-relative path that does not exist
for a pip install — and log labels must render provider display names, not
``str.title()`` artifacts (``Openrouter``, ``Zai-Coding``).
"""


def test_provider_unavailable_message_names_working_remedies():
    from modules.llm.llm_manager import LLMManager

    msg = LLMManager._provider_unavailable_message("zai-coding", ["openrouter"])
    assert "zai-coding" in msg
    assert "openrouter" in msg
    assert "polyrob doctor" in msg
    assert "polyrob config set" in msg
    assert "providers.yaml" in msg
    assert "config/.env" not in msg


def test_provider_unavailable_message_empty_available():
    from modules.llm.llm_manager import LLMManager

    msg = LLMManager._provider_unavailable_message("openai", [])
    assert "none" in msg


def test_provider_display_uses_profile_display_name():
    from modules.llm.llm_manager import LLMManager

    assert LLMManager._provider_display("openrouter") == "OpenRouter"
    assert LLMManager._provider_display("nvidia") == "NVIDIA NIM"
    # unknown names degrade gracefully (no crash, no empty string)
    assert LLMManager._provider_display("mystery") == "mystery"
