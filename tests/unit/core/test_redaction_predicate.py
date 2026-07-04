"""Behavioral tests for the display-redaction secret-name predicate.

Tests that core.secrets.is_secret_key correctly identifies secret names
for redaction purposes. A separate concern from core.bootstrap._is_secret_key,
which is a narrow backfill selector.
"""
from core.secrets import is_secret_key
import cli.commands.config as cfg
import core.bootstrap as boot


def test_known_secret_names_redacted():
    """Verify known secret names are recognized."""
    for k in ["OPENAI_API_KEY", "GEMINI_API_KEY", "MCP_GATEWAY_TOKEN", "X402_PRIVATE_KEY",
              "DB_PASSWORD", "ANYSITE_JWT", "WALLET_SEED", "SIGNING_SIGNATURE", "MNEMONIC", "POLYROB_PASSPHRASE"]:
        assert is_secret_key(k) is True, k


def test_non_secret_names_not_redacted():
    """Verify non-secret names are not recognized."""
    for k in ["GOALS_ENABLED", "DEFAULT_MODEL", "MEMORY_PREFETCH_CADENCE", "UVICORN_PORT", "POLYROB_LOCAL"]:
        assert is_secret_key(k) is False, k


def test_empty_name_is_not_secret():
    """Verify empty string is not secret."""
    assert is_secret_key("") is False


def test_config_is_secret_delegates_behaviorally():
    """Behavioral: config._is_secret must agree with the SSOT on a name the OLD hint-set also caught."""
    assert cfg._is_secret("OPENAI_API_KEY") is True
    assert cfg._is_secret("DEFAULT_MODEL") is False


def test_bootstrap_backfill_selector_stays_narrow():
    """Verify bootstrap's backfill selector remains narrow and distinct from display-redaction.

    bootstrap._is_secret_key is a DIFFERENT concern — must NOT become broad. A generic "*_TOKEN"
    that is NOT an _API_KEY and NOT allowlisted must stay non-backfillable (else we'd import
    prod secrets).
    """
    assert boot._is_secret_key("SOME_RANDOM_TOKEN") is False
    assert boot._is_secret_key("OPENAI_API_KEY") is True
    assert boot._is_secret_key("ANYSITE_JWT") is True
