"""`polyrob wallet set-cap` — owner-UX P2 T7.

C69 (2026-09-21): the DEFAULT write is now the owner PREFERENCE the live gate
re-reads (`budget.wallet_*`), and `--env` is the operator envelope write these
tests cover — two writers existed for one number and the CLI used the one a
running service does not re-read. A cap of 0 is still rejected (not treated as
"disabled" — H3, 2026-08-22: the daily cap's explicit disable action is
`set-cap daily none --env`, since an absent env var means the finite $100
default, not "no cap"; the per-tx ceiling has no disable value at all).
"""
import pytest
from click.testing import CliRunner

from cli.commands.wallet import wallet_cmd


def _invoke(args, input=None):
    return CliRunner().invoke(wallet_cmd, args, input=input)


def test_confirm_yes_writes_the_key(tmp_path):
    home = tmp_path / "home"
    res = _invoke(["set-cap", "daily", "25", "--env", "--home", str(home)], input="y\n")
    assert res.exit_code == 0, res.output
    content = (home / ".env").read_text()
    assert "WALLET_DAILY_CAP_USD=25" in content


def test_confirm_no_writes_nothing(tmp_path):
    home = tmp_path / "home"
    res = _invoke(["set-cap", "daily", "25", "--env", "--home", str(home)], input="n\n")
    assert res.exit_code == 0, res.output
    assert not (home / ".env").exists()


def test_yes_flag_bypasses_confirmation(tmp_path):
    home = tmp_path / "home"
    # No input piped at all — if the code path prompted, CliRunner would raise/hang
    # or error on empty stdin instead of proceeding.
    res = _invoke(["set-cap", "per-tx", "500", "--yes", "--env", "--home", str(home)])
    assert res.exit_code == 0, res.output
    content = (home / ".env").read_text()
    assert "AGENT_WALLET_MAX_PER_TX_USD=500" in content


def test_per_tx_writes_the_other_key_only(tmp_path):
    home = tmp_path / "home"
    res = _invoke(["set-cap", "per-tx", "12.5", "--yes", "--env", "--home", str(home)])
    assert res.exit_code == 0, res.output
    content = (home / ".env").read_text()
    assert "AGENT_WALLET_MAX_PER_TX_USD=12.5" in content
    assert "WALLET_DAILY_CAP_USD" not in content


@pytest.mark.parametrize("bad", ["0", "-5", "abc", "nan", "inf", "-inf"])
def test_rejects_non_positive_or_non_numeric(tmp_path, bad):
    home = tmp_path / "home"
    res = _invoke(["set-cap", "daily", bad, "--yes", "--env", "--home", str(home)])
    assert res.exit_code != 0
    assert not (home / ".env").exists()


def test_zero_is_not_treated_as_disable(tmp_path):
    home = tmp_path / "home"
    res = _invoke(["set-cap", "daily", "0", "--yes", "--env", "--home", str(home)])
    assert res.exit_code != 0
    # H3 (2026-08-22): "remove the env var" no longer disables the daily cap
    # (unset now means the finite $100 default) — the guidance now points at
    # the explicit disable sentinel instead.
    assert "set-cap daily none" in res.output


def test_daily_none_disables_the_cap(tmp_path):
    """H3 (2026-08-22): the ONLY way to disable the daily cap now — an absent
    env var means the finite default, not "no cap"."""
    home = tmp_path / "home"
    res = _invoke(["set-cap", "daily", "none", "--yes", "--env", "--home", str(home)])
    assert res.exit_code == 0, res.output
    content = (home / ".env").read_text()
    assert "WALLET_DAILY_CAP_USD=none" in content
    assert "DISABLED" in res.output


def test_per_tx_has_no_disable_sentinel(tmp_path):
    """Unlike daily, AGENT_WALLET_MAX_PER_TX_USD has no disable word — it is a
    required money ceiling, always a positive number."""
    home = tmp_path / "home"
    res = _invoke(["set-cap", "per-tx", "none", "--yes", "--env", "--home", str(home)])
    assert res.exit_code != 0
    assert not (home / ".env").exists()


def test_output_shows_file_new_cap_and_restart_note(tmp_path):
    home = tmp_path / "home"
    res = _invoke(["set-cap", "daily", "10", "--yes", "--env", "--home", str(home)])
    assert res.exit_code == 0, res.output
    assert str(home / ".env") in res.output
    assert "10" in res.output
    assert "restart" in res.output.lower()


def test_output_includes_policygate_tighten_only_note(tmp_path):
    # core.wallet.config.effective_daily_cap_usd / effective_max_per_tx_usd are
    # real callers wired into load_wallet_config() -> PolicyGate (owner-UX
    # G-13) and re-resolved live since 2026-09-18. The command states the two
    # merge rules: the daily cap is min-merged (a pref can only tighten it);
    # the per-tx ceiling is an owner-override clamped to the daily cap.
    home = tmp_path / "home"
    res = _invoke(["set-cap", "daily", "10", "--yes", "--env", "--home", str(home)])
    assert res.exit_code == 0, res.output
    assert "tighten only" in res.output
    assert "budget.wallet_per_tx_usd" in res.output
    assert "apply live" in res.output


def test_confirmation_prompt_shows_exact_key_value_and_path(tmp_path):
    home = tmp_path / "home"
    res = _invoke(["set-cap", "daily", "25", "--env", "--home", str(home)], input="y\n")
    assert res.exit_code == 0, res.output
    assert "WALLET_DAILY_CAP_USD=25" in res.output
    assert str(home / ".env") in res.output


def test_written_env_file_is_locked_down(tmp_path):
    home = tmp_path / "home"
    res = _invoke(["set-cap", "daily", "10", "--yes", "--env", "--home", str(home)])
    assert res.exit_code == 0, res.output
    assert (home / ".env").stat().st_mode & 0o777 == 0o600


def test_output_names_process_and_systemd_env_file(tmp_path):
    """L10: 'restart' alone is ambiguous — the output must name WHICH process
    re-reads WHICH env file (local CLI/REPL vs a systemd service's own file)."""
    home = tmp_path / "home"
    res = _invoke(["set-cap", "daily", "10", "--yes", "--env", "--home", str(home)])
    assert res.exit_code == 0, res.output
    assert "CLI/REPL" in res.output
    assert str(home / ".env") in res.output
    assert "/etc/polyrob/polyrob.env" in res.output
