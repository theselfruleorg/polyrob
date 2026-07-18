"""Pref threading: a written pref tightens; no pref == legacy (spec §3.2).

Covers the owner-UX P1 T4 read sites: goals (daily quota, max concurrent),
wallet daily cap, user-delivery rate/cap, and the owner digest enabled-flag/
channel. Every site: no pref file => byte-identical to the existing env
accessor; a written pref tightens (min-merge) or overrides (override-merge)
it per core.prefs.PREF_SCHEMA.

(The autonomy-budget read site + its `budget.autonomy_daily_usd` pref were
removed along with the autonomy budget gate itself — Task 9 of the
money-ledger split proposal.)
"""
from core.prefs import write_preference


def test_goal_quota_tightened_by_pref(tmp_path):
    from agents.task.goals.dispatcher import effective_goal_quota
    assert effective_goal_quota("u1", tmp_path) >= 1          # legacy env default
    write_preference(tmp_path, "u1", "goals.daily_quota", 2)
    assert effective_goal_quota("u1", tmp_path) == 2


def test_goal_max_concurrent_tightened_by_pref(tmp_path):
    from agents.task.goals.dispatcher import effective_goal_max_concurrent
    legacy = effective_goal_max_concurrent("u1", tmp_path)
    assert legacy >= 1
    write_preference(tmp_path, "u1", "goals.max_concurrent", 1)
    assert effective_goal_max_concurrent("u1", tmp_path) == 1


def test_goal_concurrency_zero_args_is_legacy_unchanged():
    """The pre-existing zero-arg call (test_goal_concurrency_clamp.py) must stay
    byte-identical — user_id/home_dir default to None => env-only resolution."""
    from agents.task.goals.dispatcher import (
        effective_goal_concurrency, effective_goal_max_concurrent,
    )
    assert effective_goal_concurrency() == effective_goal_max_concurrent(None, None)


# ---------------------------------------------------------------------------
# owner-UX P1 final review: 0/negative env sentinel ("disabled"/"unlimited")
# must not be fed into the min-merge as a real floor — env=0 + pref=5 must
# resolve to the owner's requested 5, not silently collapse to 0/"unlimited".
# ---------------------------------------------------------------------------


def test_goal_quota_sentinel_env_disabled_pref_alone_sets_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("GOAL_DAILY_QUOTA", "0")
    from agents.task.goals.dispatcher import effective_goal_quota
    write_preference(tmp_path, "u1", "goals.daily_quota", 5)
    assert effective_goal_quota("u1", tmp_path) == 5


def test_goal_quota_sentinel_env_disabled_no_pref_stays_legacy(tmp_path, monkeypatch):
    monkeypatch.setenv("GOAL_DAILY_QUOTA", "0")
    from agents.task.goals.dispatcher import effective_goal_quota
    assert effective_goal_quota("u1", tmp_path) == 0


def test_goal_quota_real_env_cap_wins_over_looser_pref(tmp_path, monkeypatch):
    monkeypatch.setenv("GOAL_DAILY_QUOTA", "10")
    from agents.task.goals.dispatcher import effective_goal_quota
    write_preference(tmp_path, "u1", "goals.daily_quota", 50)
    assert effective_goal_quota("u1", tmp_path) == 10


def test_goal_max_concurrent_sentinel_env_disabled_pref_alone_sets_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("GOAL_MAX_CONCURRENT", "0")
    from agents.task.goals.dispatcher import effective_goal_max_concurrent
    write_preference(tmp_path, "u1", "goals.max_concurrent", 3)
    assert effective_goal_max_concurrent("u1", tmp_path) == 3


def test_goal_max_concurrent_sentinel_env_disabled_no_pref_stays_legacy(tmp_path, monkeypatch):
    monkeypatch.setenv("GOAL_MAX_CONCURRENT", "0")
    from agents.task.goals.dispatcher import effective_goal_max_concurrent
    assert effective_goal_max_concurrent("u1", tmp_path) == 0


def test_goal_max_concurrent_real_env_cap_wins_over_looser_pref(tmp_path, monkeypatch):
    monkeypatch.setenv("GOAL_MAX_CONCURRENT", "2")
    from agents.task.goals.dispatcher import effective_goal_max_concurrent
    write_preference(tmp_path, "u1", "goals.max_concurrent", 10)
    assert effective_goal_max_concurrent("u1", tmp_path) == 2


def test_delivery_rate_tightened_by_pref(tmp_path):
    from core.surfaces.user_delivery import effective_rate_per_hour
    legacy = effective_rate_per_hour("u1", tmp_path)
    write_preference(tmp_path, "u1", "delivery.rate_per_hour", 1)
    assert effective_rate_per_hour("u1", tmp_path) == 1
    assert legacy >= 1


def test_delivery_daily_cap_tightened_by_pref(tmp_path):
    from core.surfaces.user_delivery import effective_daily_cap
    legacy = effective_daily_cap("u1", tmp_path)
    write_preference(tmp_path, "u1", "delivery.daily_cap", 1)
    assert effective_daily_cap("u1", tmp_path) == 1
    assert legacy >= 1


def test_wallet_daily_cap_min_merge(tmp_path, monkeypatch):
    monkeypatch.setenv("WALLET_DAILY_CAP_USD", "10")
    from core.wallet.config import effective_daily_cap_usd
    write_preference(tmp_path, "u1", "budget.wallet_daily_usd", 50.0)
    assert effective_daily_cap_usd("u1", tmp_path) == 10.0    # env floor holds
    write_preference(tmp_path, "u1", "budget.wallet_daily_usd", 5.0)
    assert effective_daily_cap_usd("u1", tmp_path) == 5.0


def test_wallet_daily_cap_pref_alone_sets_cap_when_env_unset(tmp_path, monkeypatch):
    """WALLET_DAILY_CAP_USD unset == legacy 'no cap' — a pref ALONE must still
    be able to set a cap (env_value=None is not treated as a ceiling of 0)."""
    monkeypatch.delenv("WALLET_DAILY_CAP_USD", raising=False)
    from core.wallet.config import effective_daily_cap_usd
    assert effective_daily_cap_usd("u1", tmp_path) is None
    write_preference(tmp_path, "u1", "budget.wallet_daily_usd", 5.0)
    assert effective_daily_cap_usd("u1", tmp_path) == 5.0


def test_wallet_per_tx_cap_min_merge(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WALLET_MAX_PER_TX_USD", "500")
    from core.wallet.config import effective_max_per_tx_usd
    write_preference(tmp_path, "u1", "budget.wallet_per_tx_usd", 800.0)
    assert effective_max_per_tx_usd("u1", tmp_path) == 500.0   # env ceiling holds
    write_preference(tmp_path, "u1", "budget.wallet_per_tx_usd", 100.0)
    assert effective_max_per_tx_usd("u1", tmp_path) == 100.0


def test_wallet_per_tx_cap_no_pref_is_legacy_env_default(tmp_path, monkeypatch):
    """Unlike the daily cap, AGENT_WALLET_MAX_PER_TX_USD unset is NOT 'no cap' —
    it's the $1000 catastrophic-loss safety default, so with no pref file the
    resolved value must equal that concrete default, never None."""
    monkeypatch.delenv("AGENT_WALLET_MAX_PER_TX_USD", raising=False)
    from core.wallet.config import effective_max_per_tx_usd
    assert effective_max_per_tx_usd("u1", tmp_path) == 1000.0
    write_preference(tmp_path, "u1", "budget.wallet_per_tx_usd", 250.0)
    assert effective_max_per_tx_usd("u1", tmp_path) == 250.0


def test_digest_enabled_overridden_by_pref(tmp_path, monkeypatch):
    monkeypatch.setenv("OWNER_DIGEST_ENABLED", "false")
    from cron.digest import digest_enabled_for
    assert digest_enabled_for("u1", tmp_path) is False
    write_preference(tmp_path, "u1", "digest.enabled", True)
    assert digest_enabled_for("u1", tmp_path) is True


def test_digest_channel_overridden_by_pref(tmp_path):
    from cron.delivery import effective_digest_channel
    assert effective_digest_channel("u1", tmp_path) == "telegram"
    write_preference(tmp_path, "u1", "digest.channel", "email")
    assert effective_digest_channel("u1", tmp_path) == "email"
