"""Regression: a malformed (present-but-invalid) key must NOT pass the gating oracle.

BotConfig.validate_api_keys blanks placeholder / <20-char keys, so the manager
rejects them. The gating oracle (should_warn / backfill / resolver) must mirror that
— else a junk key in ~/.polyrob/.env passes the guard, blocks the real-key backfill,
and the manager crashes with a misleading 'No API key found'. (Live bug, 2026-07-01.)
"""
from modules.llm.profiles import (
    looks_like_real_key,
    usable_providers_with_keys,
    initializable_providers_with_keys,
)


def test_short_key_is_not_a_real_key():
    assert looks_like_real_key("sk-tooshort") is False       # <20 chars
    assert looks_like_real_key("") is False
    assert looks_like_real_key(None) is False
    assert looks_like_real_key("your-anthropic-key") is False  # placeholder
    assert looks_like_real_key("sk-or-v1-" + "a" * 40) is True


def test_junk_anthropic_key_is_not_usable():
    # The exact live scenario: a 10-char ANTHROPIC_API_KEY that BotConfig blanks.
    env = {"ANTHROPIC_API_KEY": "sk-junk123"}  # len 10 < 20
    assert initializable_providers_with_keys(env) == ["anthropic"]  # raw presence: yes
    assert usable_providers_with_keys(env) == []                     # usable: NO


def test_real_length_key_is_usable():
    env = {"OPENROUTER_API_KEY": "sk-or-v1-" + "b" * 60}
    assert usable_providers_with_keys(env) == ["openrouter"]


def test_deepseek_never_usable_even_with_long_key():
    env = {"DEEPSEEK_API_KEY": "sk-" + "d" * 40}
    assert usable_providers_with_keys(env) == []  # non-initializable
