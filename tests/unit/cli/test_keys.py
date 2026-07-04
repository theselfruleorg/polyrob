"""Tests for the CLI key-presence guard + no-key message (Seam 1 / Phase 0a+0d)."""
from cli.keys import should_warn_no_key, no_key_message

# A well-formed dummy key (>= 20 chars, non-placeholder) — the guard mirrors BotConfig,
# which blanks too-short/placeholder keys, so gating tests must use realistic values.
_OK = "sk-realkey-0123456789abcdef"


def test_warns_when_no_provider_key():
    assert should_warn_no_key({}) is True


def test_does_not_warn_with_openrouter_only():
    # The reported bug: an OpenRouter-only setup must NOT warn (old check missed it).
    assert should_warn_no_key({"OPENROUTER_API_KEY": _OK}) is False


def test_does_not_warn_with_any_of_five_initializable_providers():
    # deepseek is EXCLUDED — its key alone can't bootstrap a client (see below).
    for key in (
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
        "OPENROUTER_API_KEY", "NVIDIA_API_KEY",
    ):
        assert should_warn_no_key({key: _OK}) is False, key


def test_deepseek_only_still_warns():
    # A DEEPSEEK_API_KEY alone can't bootstrap (direct client disabled) — must warn,
    # NOT silently pass the guard and then hard-crash at container build.
    assert should_warn_no_key({"DEEPSEEK_API_KEY": _OK}) is True


def test_malformed_short_key_still_warns():
    # Live bug 2026-07-01: a too-short ANTHROPIC key (BotConfig blanks <20 chars) must
    # NOT pass the guard — it would block the real-key backfill and crash the manager.
    assert should_warn_no_key({"ANTHROPIC_API_KEY": "sk-junk123"}) is True


def test_message_mentions_openrouter_for_deepseek():
    msg = no_key_message()
    assert "deepseek/deepseek-chat" in msg
    assert "OPENROUTER_API_KEY" in msg


def test_keyless_rob_env_still_warns():
    # Phase 0d: a keyless ~/.polyrob/.env must not suppress the warning. The decision
    # is driven purely by whether any provider key is present, not by file existence.
    assert should_warn_no_key({}) is True


def test_message_names_all_six_providers():
    msg = no_key_message()
    for prov in ("ANTHROPIC", "OPENAI", "GEMINI", "DEEPSEEK", "OPENROUTER", "NVIDIA"):
        assert prov in msg, prov


def test_message_names_accepted_locations_and_init():
    msg = no_key_message()
    assert "rob init" in msg
    for loc in ("~/.polyrob/.env", ".polyrob/.env", "config/.env.production"):
        assert loc in msg
