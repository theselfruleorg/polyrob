"""T1-10 (2026-07-06 structural review): the per-step brain-state format nag
(INJECT_FORMAT_HINT_EARLY, native branch) was injected for every provider —
including Anthropic/Claude, though the family-note design (MODEL_FAMILY_
INSTRUCTIONS) deliberately gives Claude none because it doesn't need it.

Gate the nag on the same family-needle mechanism: Claude/Anthropic is exempt;
weak families (kimi/gemini/gpt/grok/...) keep the reminder.
"""
from agents.task.constants import format_nag_exempt


def test_claude_models_exempt():
    assert format_nag_exempt("claude-sonnet-5") is True
    assert format_nag_exempt("claude-opus-4-8", "anthropic") is True


def test_anthropic_provider_exempt_regardless_of_name():
    assert format_nag_exempt("some-gateway-alias", "anthropic") is True


def test_weak_families_keep_the_nag():
    for name in ("kimi-k2", "gemini-3-pro", "gpt-5.2", "grok-4.3", "deepseek-chat", ""):
        assert format_nag_exempt(name) is False, name


def test_nag_wired_through_family_gate():
    import inspect

    from agents.task.agent.core import next_action_internal

    src = inspect.getsource(next_action_internal)
    assert "format_nag_exempt" in src
