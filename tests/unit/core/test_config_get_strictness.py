"""Tests for BotConfig.get and memory feature-flag env-backing (Task 0.3).

Construction strategy: BotConfig uses pydantic-settings BaseSettings. We pass
field values as kwargs to BotConfig() to avoid depending on the environment.

Note on worktree isolation: when pytest runs the full test/ suite from this git
worktree, the shared venv may load core.config from the main checkout first.
Our conftest.py re-routes sys.path before this module is collected, so imports
here see the worktree's BotConfig (which has the new fields + validator).
"""
from core.config import BotConfig


def test_hierarchical_memory_flag_reads_env():
    """HIERARCHICAL_MEMORY_ENABLED=false must be honoured by the field, not swallowed."""
    cfg = BotConfig(HIERARCHICAL_MEMORY_ENABLED=False)
    assert cfg.get("HIERARCHICAL_MEMORY_ENABLED", True) is False, (
        "HIERARCHICAL_MEMORY_ENABLED=false must be honored, not swallowed by getattr default"
    )


def test_memory_flags_default_true_when_unset():
    """All memory flags default to True when not overridden."""
    cfg = BotConfig()
    assert cfg.get("COMPACTION_ENABLED", False) is True
    assert cfg.get("HIERARCHICAL_MEMORY_ENABLED", False) is True
    assert cfg.get("SEMANTIC_RETRIEVAL_ENABLED", False) is True
    assert cfg.get("REFLECTION_ENABLED", False) is True
    assert cfg.get("FORGETTING_ENABLED", False) is True


def test_memory_flag_off_string():
    """False value maps to False."""
    cfg = BotConfig(COMPACTION_ENABLED=False)
    assert cfg.get("COMPACTION_ENABLED", True) is False


def test_memory_flag_none_string():
    """False value maps to False."""
    cfg = BotConfig(SEMANTIC_RETRIEVAL_ENABLED=False)
    assert cfg.get("SEMANTIC_RETRIEVAL_ENABLED", True) is False


def test_memory_flag_true_string():
    """Explicit True passes through correctly."""
    cfg = BotConfig(FORGETTING_ENABLED=True)
    assert cfg.get("FORGETTING_ENABLED", False) is True


def test_coerce_memory_flag_validator_off():
    """Validator coerces 'off' string to False before field assignment."""
    result = BotConfig._coerce_mem("off")
    assert result is False


def test_coerce_memory_flag_validator_none_str():
    """Validator coerces 'none' string to False before field assignment."""
    result = BotConfig._coerce_mem("none")
    assert result is False


def test_coerce_memory_flag_validator_false_str():
    """Validator coerces 'false' string to False before field assignment."""
    result = BotConfig._coerce_mem("false")
    assert result is False


def test_coerce_memory_flag_validator_true_str():
    """Validator coerces 'true' string to True before field assignment."""
    result = BotConfig._coerce_mem("true")
    assert result is True


def test_coerce_memory_flag_validator_bool_passthrough():
    """Validator passes bool values through unchanged."""
    assert BotConfig._coerce_mem(True) is True
    assert BotConfig._coerce_mem(False) is False


def test_get_unknown_key_returns_default():
    """Unknown keys should still return default (no raise), but we observe via debug log."""
    cfg = BotConfig()
    result = cfg.get("TOTALLY_NONEXISTENT_KEY_XYZ", "sentinel")
    assert result == "sentinel"


def test_get_known_declared_field():
    """get() still works for normal declared fields."""
    cfg = BotConfig()
    # cache_ttl is a declared field with default 3600
    assert cfg.get("cache_ttl", -1) == 3600
