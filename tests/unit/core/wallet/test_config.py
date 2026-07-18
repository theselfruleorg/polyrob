import pytest

from core.wallet.config import load_wallet_config, TESTNET_FACILITATOR_URL
from core.prefs import write_preference

# _isolate_polyrob_home (G-13) lives in the directory-level conftest.py — it
# covers every file here, not just this one (test_per_venue_cap.py /
# test_operational_venue.py also call load_wallet_config(env) zero-arg).


def test_defaults_are_safe():
    cfg = load_wallet_config({})
    assert cfg.enabled is False
    assert cfg.x402_client_enabled is False
    assert cfg.network == "testnet"
    assert cfg.backend == "local_eoa"
    assert cfg.master_seed is None
    assert cfg.x402_facilitator_url == TESTNET_FACILITATOR_URL
    assert cfg.daily_cap_usd is None  # disabled by default = legacy behavior


def test_repr_excludes_master_seed():
    """L1: WalletConfig's auto-repr must never embed the raw seed — one
    logger.debug(f"...{cfg}") away from a log leak."""
    marker = "SEEDMARKER" + "x" * 40
    cfg = load_wallet_config({"AGENT_WALLET_ENABLED": "true",
                              "AGENT_WALLET_MASTER_SEED": marker})
    r = repr(cfg)
    assert marker not in r
    assert "master_seed" not in r  # field omitted from repr entirely


def test_daily_cap_parsed_when_set():
    assert load_wallet_config({"WALLET_DAILY_CAP_USD": "5"}).daily_cap_usd == 5.0
    # blank / unparseable → disabled (None), never a crash
    assert load_wallet_config({"WALLET_DAILY_CAP_USD": ""}).daily_cap_usd is None
    assert load_wallet_config({"WALLET_DAILY_CAP_USD": "abc"}).daily_cap_usd is None


def test_reads_env():
    env = {
        "AGENT_WALLET_ENABLED": "true",
        "AGENT_WALLET_MASTER_SEED": "x" * 40,
        "AGENT_WALLET_NETWORK": "mainnet",
        "AGENT_WALLET_MAX_PER_TX_USD": "250",
        "X402_CLIENT_ENABLED": "true",
        "X402_CLIENT_FACILITATOR_URL": "https://facilitator.example",
    }
    cfg = load_wallet_config(env)
    assert cfg.enabled is True
    assert cfg.network == "mainnet"
    assert cfg.max_per_tx_usd == 250.0
    assert cfg.x402_client_enabled is True
    assert cfg.x402_facilitator_url == "https://facilitator.example"


# --- G-13: load_wallet_config() (-> PolicyGate) is now a real caller of the
# tighten-only pref/env merge helpers. Matrix per cap: env-only / pref-only /
# both (min wins) / pref invalid or the prefs module raising (env wins, no
# crash). user_id/home_dir are passed explicitly here for a hermetic pref
# store; the zero-arg tests above (no user_id/home_dir) pin that the
# fail-open owner/home resolution never disturbs a bare env-only call. ------

def test_daily_cap_env_only_no_pref_file(tmp_path):
    env = {"WALLET_DAILY_CAP_USD": "10"}
    cfg = load_wallet_config(env, user_id="u1", home_dir=tmp_path)
    assert cfg.daily_cap_usd == 10.0


def test_daily_cap_pref_only_sets_cap_when_env_unset(tmp_path):
    write_preference(tmp_path, "u1", "budget.wallet_daily_usd", 7.0)
    cfg = load_wallet_config({}, user_id="u1", home_dir=tmp_path)
    assert cfg.daily_cap_usd == 7.0


def test_daily_cap_both_min_wins(tmp_path):
    write_preference(tmp_path, "u1", "budget.wallet_daily_usd", 50.0)
    cfg = load_wallet_config({"WALLET_DAILY_CAP_USD": "10"}, user_id="u1", home_dir=tmp_path)
    assert cfg.daily_cap_usd == 10.0  # env is tighter here -> env wins
    write_preference(tmp_path, "u1", "budget.wallet_daily_usd", 3.0)
    cfg = load_wallet_config({"WALLET_DAILY_CAP_USD": "10"}, user_id="u1", home_dir=tmp_path)
    assert cfg.daily_cap_usd == 3.0  # pref is tighter here -> pref wins


def test_daily_cap_pref_module_raising_env_wins_no_crash(tmp_path, monkeypatch):
    import core.wallet.config as wallet_config_mod

    def _boom(*a, **k):
        raise RuntimeError("prefs store unavailable")

    monkeypatch.setattr(wallet_config_mod, "effective_daily_cap_usd", _boom)
    cfg = load_wallet_config({"WALLET_DAILY_CAP_USD": "10"}, user_id="u1", home_dir=tmp_path)
    assert cfg.daily_cap_usd == 10.0  # fail-open: plain env value, no crash


def test_per_tx_cap_env_only_no_pref_file(tmp_path):
    env = {"AGENT_WALLET_MAX_PER_TX_USD": "500"}
    cfg = load_wallet_config(env, user_id="u1", home_dir=tmp_path)
    assert cfg.max_per_tx_usd == 500.0


def test_per_tx_cap_pref_only_tightens_the_safety_default(tmp_path):
    # unlike the daily cap, unset per-tx has a concrete $1000 default, not None
    write_preference(tmp_path, "u1", "budget.wallet_per_tx_usd", 200.0)
    cfg = load_wallet_config({}, user_id="u1", home_dir=tmp_path)
    assert cfg.max_per_tx_usd == 200.0


def test_per_tx_cap_both_min_wins(tmp_path):
    write_preference(tmp_path, "u1", "budget.wallet_per_tx_usd", 800.0)
    cfg = load_wallet_config({"AGENT_WALLET_MAX_PER_TX_USD": "500"}, user_id="u1", home_dir=tmp_path)
    assert cfg.max_per_tx_usd == 500.0  # env is tighter -> env wins
    write_preference(tmp_path, "u1", "budget.wallet_per_tx_usd", 100.0)
    cfg = load_wallet_config({"AGENT_WALLET_MAX_PER_TX_USD": "500"}, user_id="u1", home_dir=tmp_path)
    assert cfg.max_per_tx_usd == 100.0  # pref is tighter -> pref wins


def test_per_tx_cap_pref_invalid_falls_back_to_env(tmp_path):
    # write_preference validates at write time (min_value=0.0) — a negative
    # value is refused outright, so no bad pref ever reaches disk to begin with.
    ok, err = write_preference(tmp_path, "u1", "budget.wallet_per_tx_usd", -5.0)
    assert ok is False and err
    cfg = load_wallet_config({"AGENT_WALLET_MAX_PER_TX_USD": "500"}, user_id="u1", home_dir=tmp_path)
    assert cfg.max_per_tx_usd == 500.0


def test_per_tx_cap_pref_module_raising_env_wins_no_crash(tmp_path, monkeypatch):
    import core.wallet.config as wallet_config_mod

    def _boom(*a, **k):
        raise RuntimeError("prefs store unavailable")

    monkeypatch.setattr(wallet_config_mod, "effective_max_per_tx_usd", _boom)
    cfg = load_wallet_config({"AGENT_WALLET_MAX_PER_TX_USD": "500"}, user_id="u1", home_dir=tmp_path)
    assert cfg.max_per_tx_usd == 500.0  # fail-open: plain env value, no crash


def test_zero_arg_call_unaffected_by_fail_open_owner_home_resolution():
    """No user_id/home_dir passed => internal fail-open owner/home resolution
    kicks in, but with no real preferences.toml for that (resolved) tenant this
    must stay byte-identical to the plain env value (regression guard for the
    process-level zero-arg call sites in core/wallet/factory.py)."""
    env = {"WALLET_DAILY_CAP_USD": "10", "AGENT_WALLET_MAX_PER_TX_USD": "250"}
    cfg = load_wallet_config(env)
    assert cfg.daily_cap_usd == 10.0
    assert cfg.max_per_tx_usd == 250.0
