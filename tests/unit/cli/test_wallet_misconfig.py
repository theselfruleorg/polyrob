"""Friendly enabled-but-unseeded failure + derivation/backup line.

MONEY-CRITICAL UX: `get_agent_wallet()` raises ValueError (fail-fast, kept
intact in the factory) when AGENT_WALLET_ENABLED=true but
AGENT_WALLET_MASTER_SEED is missing/short. The bare `polyrob wallet` view and
`polyrob doctor` must turn that into a friendly, actionable message pointing
at `polyrob wallet init` — never a raw traceback.
"""
import pytest
from click.testing import CliRunner
from cli.commands.wallet import wallet_cmd
from cli.commands.doctor import doctor_report


@pytest.fixture(autouse=True)
def _reset_wallet_cache():
    from core.wallet.factory import reset_agent_wallet_cache
    reset_agent_wallet_cache()
    yield
    reset_agent_wallet_cache()


def test_bare_wallet_view_friendly_when_enabled_without_seed(monkeypatch):
    # No env-file bootstrap here — pin the seed as genuinely absent (a real
    # ~/.polyrob/.env on a dev box must not backfill it now that the group
    # callback loads env, C2).
    monkeypatch.setattr("core.bootstrap.load_env", lambda *a, **k: None)
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.delenv("AGENT_WALLET_MASTER_SEED", raising=False)
    runner = CliRunner()
    result = runner.invoke(wallet_cmd, [])
    assert result.exit_code == 0          # friendly message, not a traceback
    assert "wallet init" in result.output


def test_bare_wallet_view_bootstraps_local_env(monkeypatch):
    """C2: `polyrob wallet` must load the local env (like export/init/owner do)
    so a seed written to ~/.polyrob/.env by `wallet init` in a PRIOR process is
    seen — instead of reporting 'not enabled' while `doctor` says 'on'."""
    # Simulate a fresh process: the seed lives only in the env FILE, not os.environ.
    monkeypatch.delenv("AGENT_WALLET_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_WALLET_MASTER_SEED", raising=False)

    def _fake_load_env(*a, **k):
        monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
        monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", "w" * 40)

    monkeypatch.setattr("core.bootstrap.load_env", _fake_load_env)
    runner = CliRunner()
    result = runner.invoke(wallet_cmd, ["--no-balances"])
    assert result.exit_code == 0, result.output
    assert "not enabled" not in result.output
    assert "derivation=" in result.output   # the wallet actually resolved


def test_doctor_reports_enabled_without_seed():
    lines = doctor_report({"AGENT_WALLET_ENABLED": "true"})
    joined = "\n".join(lines)
    assert "wallet" in joined.lower()
    assert "wallet init" in joined


def test_doctor_wallet_misconfigured_on_bad_bip44_seed():
    """H14c: doctor must not report green 'on' for a wallet that crashes on use.
    A >=32-char junk seed passes the length check but fails bip44 derivation."""
    import pytest
    pytest.importorskip("eth_account")
    lines = doctor_report({
        "AGENT_WALLET_ENABLED": "true",
        "AGENT_WALLET_MASTER_SEED": "w" * 48,   # >=32 chars but not a valid mnemonic
        "AGENT_WALLET_DERIVATION": "bip44",
    })
    joined = "\n".join(lines).lower()
    assert "misconfigured" in joined
    assert "wallet: on" not in joined


def test_doctor_wallet_on_shows_network_and_derivation():
    lines = doctor_report({
        "AGENT_WALLET_ENABLED": "true",
        "AGENT_WALLET_MASTER_SEED": "x" * 40,
        "AGENT_WALLET_DERIVATION": "legacy",
        "AGENT_WALLET_NETWORK": "testnet",
    })
    joined = "\n".join(lines)
    assert "wallet: on" in joined
    assert "derivation=legacy" in joined
    assert "network=testnet" in joined


def test_bare_wallet_view_surfaces_real_error_when_seed_is_fine(monkeypatch):
    """Finding 3 regression (2026-07-14 final review): a ValueError from a cause
    OTHER than a missing/short seed (e.g. a malformed numeric env var) must not
    be mislabeled "seed missing/short" — the actual error must surface."""
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", "w" * 40)
    monkeypatch.setenv("AGENT_WALLET_MAX_PER_TX_USD", "not-a-number")
    runner = CliRunner()
    result = runner.invoke(wallet_cmd, [])
    assert result.exit_code == 0, result.output
    assert "seed missing/short" not in result.output
    assert "could not convert" in result.output.lower()


def test_bare_wallet_view_friendly_on_invalid_bip44_seed(monkeypatch):
    """H14d: an invalid bip44 seed makes lazy key derivation raise — the bare view
    must show a friendly message, not dump a raw traceback."""
    import pytest
    pytest.importorskip("eth_account")
    monkeypatch.setattr("core.bootstrap.load_env", lambda *a, **k: None)
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", "w" * 48)
    monkeypatch.setenv("AGENT_WALLET_DERIVATION", "bip44")
    runner = CliRunner()
    result = runner.invoke(wallet_cmd, ["--no-balances"])
    assert result.exit_code == 0, result.output
    assert "misconfigured" in result.output.lower()
    assert "Traceback" not in result.output


def test_bare_wallet_view_shows_derivation(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", "w" * 40)
    runner = CliRunner()
    result = runner.invoke(wallet_cmd, ["--no-balances"])
    assert result.exit_code == 0, result.output
    assert "derivation=" in result.output


def test_bare_wallet_view_disabled_with_bad_cap_says_not_enabled(monkeypatch):
    """M12: a DISABLED wallet + a malformed numeric cap env must say 'not enabled'
    (NOT 'ENABLED but seed missing'), and NAME the malformed key — the config
    raise happens before enabled/seed are read, so both were previously lies."""
    monkeypatch.setattr("core.bootstrap.load_env", lambda *a, **k: None)
    monkeypatch.delenv("AGENT_WALLET_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_WALLET_MASTER_SEED", raising=False)
    monkeypatch.setenv("AGENT_WALLET_MAX_PER_TX_USD", "abc")
    runner = CliRunner()
    result = runner.invoke(wallet_cmd, ["--no-balances"])
    assert result.exit_code == 0, result.output
    assert "not enabled" in result.output.lower()
    assert "ENABLED but" not in result.output
    assert "AGENT_WALLET_MAX_PER_TX_USD" in result.output


def test_bare_wallet_view_enabled_with_bad_cap_names_key(monkeypatch):
    """M12: an ENABLED wallet with a malformed cap env names the offending key in
    the config-error branch, not a bare ValueError blamed on the seed."""
    monkeypatch.setattr("core.bootstrap.load_env", lambda *a, **k: None)
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", "w" * 40)
    monkeypatch.setenv("AGENT_WALLET_MAX_PER_TX_USD", "abc")
    runner = CliRunner()
    result = runner.invoke(wallet_cmd, ["--no-balances"])
    assert result.exit_code == 0, result.output
    assert "AGENT_WALLET_MAX_PER_TX_USD" in result.output
    assert "seed missing/short" not in result.output


def test_bare_wallet_view_annotates_unlimited_daily(monkeypatch):
    """M13: the view annotates the per-tx CEILING and warns daily none = UNLIMITED,
    with the set-cap hint — the real posture a new owner never saw."""
    monkeypatch.setattr("core.bootstrap.load_env", lambda *a, **k: None)
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", "x" * 40)
    monkeypatch.setenv("AGENT_WALLET_DERIVATION", "legacy")  # PBKDF2 — always derives
    monkeypatch.delenv("WALLET_DAILY_CAP_USD", raising=False)
    runner = CliRunner()
    result = runner.invoke(wallet_cmd, ["--no-balances"])
    assert result.exit_code == 0, result.output
    assert "UNLIMITED" in result.output
    assert "CEILING" in result.output
    assert "set-cap daily" in result.output


def test_doctor_wallet_on_reports_caps_and_unlimited_warning():
    """H14c: doctor's 'wallet: on' line reports caps and flags no-daily-cap as
    the real (UNLIMITED) posture."""
    lines = doctor_report({
        "AGENT_WALLET_ENABLED": "true",
        "AGENT_WALLET_MASTER_SEED": "x" * 40,
        "AGENT_WALLET_DERIVATION": "legacy",
    })
    joined = "\n".join(lines)
    assert "wallet: on" in joined
    assert "caps max $1000/tx" in joined
    assert "UNLIMITED" in joined
    assert "no daily cap" in joined.lower()
