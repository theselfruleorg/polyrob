"""AUTONOMY_MODE master switch (proposal 013). Patch env via monkeypatch — never reload."""
from agents.task import constants


def _enable_full(monkeypatch):
    monkeypatch.setenv("AUTONOMY_MODE", "autonomous")
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    constants.reset_autonomy_mode_warnings()


def test_default_is_supervised(monkeypatch):
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)
    assert constants.autonomy_mode() == "supervised"
    assert constants.full_autonomy_enabled() is False


def test_unknown_value_degrades_to_supervised(monkeypatch):
    monkeypatch.setenv("AUTONOMY_MODE", "bogus")  # rejected naming must NOT work
    assert constants.autonomy_mode() == "supervised"


def test_autonomous_without_local_mode_clamps(monkeypatch):
    monkeypatch.setenv("AUTONOMY_MODE", "autonomous")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("ROB_LOCAL", raising=False)
    constants.reset_autonomy_mode_warnings()
    assert constants.full_autonomy_enabled() is False


def test_autonomous_without_owner_clamps(monkeypatch):
    monkeypatch.setenv("AUTONOMY_MODE", "autonomous")
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    for var in ("POLYROB_OWNER_USER_ID", "BOT_OWNER_USER_ID",
                "SURFACE_SUPER_ADMIN_USER_IDS",
                "POLYROB_OWNER_TELEGRAM_ID", "ALLOWED_TELEGRAM_USER_IDS",
                "POLYROB_OWNER_EMAIL", "BOT_OWNER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    constants.reset_autonomy_mode_warnings()
    assert constants.full_autonomy_enabled() is False


def test_full_autonomy_when_local_and_owner_bound(monkeypatch):
    _enable_full(monkeypatch)
    assert constants.full_autonomy_enabled() is True


def test_posture_default_full_under_autonomous_mode(monkeypatch):
    _enable_full(monkeypatch)
    monkeypatch.delenv("AUTONOMY_POSTURE", raising=False)
    assert constants.autonomy_posture() == "full"


def test_explicit_posture_env_beats_mode(monkeypatch):
    _enable_full(monkeypatch)
    monkeypatch.setenv("AUTONOMY_POSTURE", "silent")
    assert constants.autonomy_posture() == "silent"


def test_posture_default_unchanged_supervised(monkeypatch):
    """Regression: AUTONOMY_MODE and AUTONOMY_POSTURE both unset -> byte-identical
    'silent' default, unaffected by this axis existing at all."""
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)
    monkeypatch.delenv("AUTONOMY_POSTURE", raising=False)
    assert constants.autonomy_posture() == "silent"


def test_mode_capability_default_membership(monkeypatch):
    _enable_full(monkeypatch)
    assert constants._mode_capability_default("TWITTER_ENABLED") is True
    assert constants._mode_capability_default("X402_CLIENT_ENABLED") is False  # money: never


def test_money_spend_flags_unaffected(monkeypatch):
    """The absolute hard line: autonomous mode changes NO money-spend default."""
    _enable_full(monkeypatch)
    from core.env import bool_env
    for flag in ("X402_CLIENT_ENABLED", "AGENT_WALLET_ENABLED",
                 "HYPERLIQUID_TRADING_ENABLED", "POLYMARKET_TRADING_ENABLED",
                 "CRYPTO_TRADE_LIVE_ENABLED"):
        monkeypatch.delenv(flag, raising=False)
        assert bool_env(flag, False) is False
        assert flag not in constants._MODE_CAPABILITY_FLAGS
