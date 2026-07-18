"""`polyrob wallet init` — one-command create/import, BIP-44 default, earnings link.

MONEY-CRITICAL: the mnemonic must be printed exactly once with the backup
warning; init refuses when a seed is already configured; `--from-seed`
(legacy import) keeps a pre-BIP44 install's addresses by recording scheme
"legacy".
"""
import json
import os
import pytest
from click.testing import CliRunner
from cli.commands.wallet import wallet_cmd, run_wallet_init_flow


MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    # Never touch the real ~/.polyrob or data/wallet in tests.
    monkeypatch.delenv("AGENT_WALLET_MASTER_SEED", raising=False)
    monkeypatch.delenv("AGENT_WALLET_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_WALLET_DERIVATION", raising=False)
    monkeypatch.delenv("X402_PAYMENT_RECIPIENT", raising=False)
    yield
    # run_wallet_init_flow mutates os.environ directly (not via monkeypatch),
    # so monkeypatch's own teardown won't undo it — and delenv(raising=False)
    # records no undo when the var was absent beforehand. Pop explicitly so
    # these vars never leak into later test files in the same pytest session.
    # AGENT_WALLET_DERIVATION included (H1 made init flow set it): a leaked
    # bip44 pin makes any later junk-seed test derive → raise → false failures.
    os.environ.pop("AGENT_WALLET_MASTER_SEED", None)
    os.environ.pop("AGENT_WALLET_ENABLED", None)
    os.environ.pop("AGENT_WALLET_DERIVATION", None)
    os.environ.pop("X402_PAYMENT_RECIPIENT", None)


def test_init_flow_generates_bip44_wallet(tmp_path):
    pytest.importorskip("eth_account")
    result = run_wallet_init_flow(mnemonic=None, raw_seed=None, home=tmp_path,
                                  assume_yes=True, data_dir=tmp_path / "wallet")
    env_text = (tmp_path / ".env").read_text()
    assert "AGENT_WALLET_ENABLED=true" in env_text
    assert "AGENT_WALLET_MASTER_SEED=" in env_text
    meta = json.loads((tmp_path / "wallet" / "meta.json").read_text())
    assert meta["derivation"] == "bip44"
    assert result["address"].startswith("0x")
    assert result["scheme"] == "bip44"


def test_init_pins_derivation_scheme_in_env(tmp_path):
    """H1: init writes AGENT_WALLET_DERIVATION alongside the seed in the GLOBAL
    .env, so the scheme travels with the seed and resolve_scheme is CWD-independent.
    Otherwise a bip44 wallet whose meta.json lives under a CWD-relative data-home
    silently flips to 'legacy' (wrong funded address) when run from another dir."""
    pytest.importorskip("eth_account")
    run_wallet_init_flow(mnemonic=MNEMONIC, raw_seed=None, home=tmp_path,
                         assume_yes=True, data_dir=tmp_path / "wallet")
    assert "AGENT_WALLET_DERIVATION=bip44" in (tmp_path / ".env").read_text()


def test_init_legacy_pins_derivation_scheme_in_env(tmp_path):
    run_wallet_init_flow(mnemonic=None, raw_seed="y" * 40, home=tmp_path,
                         assume_yes=True, data_dir=tmp_path / "wallet")
    assert "AGENT_WALLET_DERIVATION=legacy" in (tmp_path / ".env").read_text()


def test_init_flow_import_mnemonic_deterministic(tmp_path):
    pytest.importorskip("eth_account")
    r = run_wallet_init_flow(mnemonic=MNEMONIC, raw_seed=None, home=tmp_path,
                             assume_yes=True, data_dir=tmp_path / "wallet")
    from core.wallet.derivation import derive_key
    from core.wallet.signer import LocalEoaSigner
    assert r["address"] == LocalEoaSigner(derive_key(MNEMONIC, "treasury", "bip44")).address


def test_init_flow_import_raw_seed_is_legacy(tmp_path):
    r = run_wallet_init_flow(mnemonic=None, raw_seed="y" * 40, home=tmp_path,
                             assume_yes=True, data_dir=tmp_path / "wallet")
    assert r["scheme"] == "legacy"
    meta = json.loads((tmp_path / "wallet" / "meta.json").read_text())
    assert meta["derivation"] == "legacy"


def test_init_flow_refuses_when_seed_already_set(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", "z" * 40)
    with pytest.raises(Exception):  # click.ClickException
        run_wallet_init_flow(mnemonic=None, raw_seed=None, home=tmp_path,
                             assume_yes=True, data_dir=tmp_path / "wallet")


def test_init_flow_aborts_before_persisting_seed_on_scheme_conflict(tmp_path):
    """Finding 2 regression (2026-07-14 final review): if write_scheme_once()
    raises (conflicting existing meta), NOTHING must be written/printed yet.
    Before the fix, the seed was persisted to .env and the address printed
    BEFORE the scheme check ran, so a conflict left a persisted seed whose
    printed address was inconsistent with the scheme the runtime resolves.
    """
    pytest.importorskip("eth_account")
    from core.wallet import derivation
    data_dir = tmp_path / "wallet"
    derivation.write_scheme_once("legacy", data_dir=data_dir)  # pre-existing conflicting meta
    env_path = tmp_path / ".env"
    assert not env_path.exists()

    with pytest.raises(Exception):  # generate path defaults to "bip44" -> conflicts with "legacy"
        run_wallet_init_flow(mnemonic=None, raw_seed=None, home=tmp_path,
                             assume_yes=True, data_dir=data_dir)

    assert not env_path.exists()
    assert not (os.environ.get("AGENT_WALLET_MASTER_SEED") or "")


def test_init_flow_rejects_short_raw_seed(tmp_path):
    with pytest.raises(Exception):
        run_wallet_init_flow(mnemonic=None, raw_seed="short", home=tmp_path,
                             assume_yes=True, data_dir=tmp_path / "wallet")


def test_cli_init_subcommand_yes(tmp_path, monkeypatch):
    pytest.importorskip("eth_account")
    # cli/commands/wallet.py does `from core.paths import polyrob_home` at
    # module import time, so monkeypatching `core.paths.polyrob_home` would
    # NOT be seen by `wallet_init_cmd` (it already holds its own bound
    # reference). Use the existing hidden `--home` option instead (same
    # pattern as `set-cap`'s tests) — cleaner than patching the wrong name.
    # The command also imports `load_env` locally (`from core.bootstrap import
    # load_env`) and calls it with local_mode=True, which would otherwise read
    # the developer's real ~/.polyrob/.env — patch the origin so the test
    # stays hermetic regardless of what's configured on the dev machine.
    monkeypatch.setattr("core.bootstrap.load_env", lambda *a, **k: None)
    # L2: the generated mnemonic is only printed on a TTY. Force the CliRunner's
    # stream wrapper to report isatty()=True so this test exercises the TTY path
    # (the CliRunner default is non-TTY → redacted; see the redaction test below).
    monkeypatch.setattr("click.testing._NamedTextIOWrapper.isatty", lambda self: True)
    runner = CliRunner()
    result = runner.invoke(wallet_cmd, ["init", "--yes", "--home", str(tmp_path),
                                        "--data-dir", str(tmp_path / "w")])
    assert result.exit_code == 0, result.output
    assert "Fund THIS address" in result.output or "0x" in result.output
    # mnemonic must be shown exactly once with the backup warning
    assert "back" in result.output.lower()


def test_cli_init_redacts_mnemonic_on_non_tty(tmp_path, monkeypatch):
    """L2: `polyrob wallet init --yes > file` must NOT write the mnemonic to a
    non-TTY stdout — it redacts and points at the interactive export."""
    pytest.importorskip("eth_account")
    monkeypatch.setattr("core.bootstrap.load_env", lambda *a, **k: None)
    runner = CliRunner()  # CliRunner streams report isatty()=False by default
    result = runner.invoke(wallet_cmd, ["init", "--yes", "--home", str(tmp_path),
                                        "--data-dir", str(tmp_path / "w")])
    assert result.exit_code == 0, result.output
    # The env file has the seed, but stdout must not.
    seed_line = [ln for ln in (tmp_path / ".env").read_text().splitlines()
                 if ln.startswith("AGENT_WALLET_MASTER_SEED=")][0]
    seed = seed_line.split("=", 1)[1]
    assert seed not in result.output
    assert "not printed" in result.output.lower() or "not a tty" in result.output.lower()
    assert "wallet export" in result.output


def test_cli_init_yes_echoes_recipient_link(tmp_path, monkeypatch):
    """M15: under --yes the X402_PAYMENT_RECIPIENT write must be echoed, not silent."""
    pytest.importorskip("eth_account")
    monkeypatch.setattr("core.bootstrap.load_env", lambda *a, **k: None)
    monkeypatch.delenv("X402_PAYMENT_RECIPIENT", raising=False)
    runner = CliRunner()
    result = runner.invoke(wallet_cmd, ["init", "--yes", "--home", str(tmp_path),
                                        "--data-dir", str(tmp_path / "w")])
    assert result.exit_code == 0, result.output
    assert "X402_PAYMENT_RECIPIENT" in result.output
    assert "Linked earnings" in result.output


def test_cli_init_from_mnemonic_empty_prompts_hidden(tmp_path, monkeypatch):
    """M14: `--from-mnemonic ""` (empty value) triggers a HIDDEN prompt so the
    master secret never lands on the command line / shell history."""
    pytest.importorskip("eth_account")
    monkeypatch.setattr("core.bootstrap.load_env", lambda *a, **k: None)
    monkeypatch.setattr("click.testing._NamedTextIOWrapper.isatty", lambda self: True)
    runner = CliRunner()
    result = runner.invoke(
        wallet_cmd,
        ["init", "--from-mnemonic", "", "--yes", "--home", str(tmp_path),
         "--data-dir", str(tmp_path / "w")],
        input=MNEMONIC + "\n")
    assert result.exit_code == 0, result.output
    env_text = (tmp_path / ".env").read_text()
    assert "AGENT_WALLET_DERIVATION=bip44" in env_text  # the mnemonic was imported
    from core.wallet.derivation import derive_key
    from core.wallet.signer import LocalEoaSigner
    expected = LocalEoaSigner(derive_key(MNEMONIC, "treasury", "bip44")).address
    assert expected in result.output


def test_cli_init_from_mnemonic_empty_refuses_non_tty(tmp_path, monkeypatch):
    """M14: an empty `--from-mnemonic` on a non-TTY stdin is refused (never read a
    secret non-interactively)."""
    monkeypatch.setattr("core.bootstrap.load_env", lambda *a, **k: None)
    runner = CliRunner()  # non-TTY stdin by default
    result = runner.invoke(wallet_cmd, ["init", "--from-mnemonic", "", "--yes",
                                        "--home", str(tmp_path)])
    assert result.exit_code != 0
    assert "tty" in result.output.lower() or "non-interactively" in result.output.lower()


def test_cli_init_shows_caps_and_setcap_hint(tmp_path, monkeypatch):
    """M13: init surfaces the real spend posture (ceiling + unlimited daily) and
    the set-cap hint, so a new owner isn't silently left at $1000/tx-unlimited."""
    pytest.importorskip("eth_account")
    monkeypatch.setattr("core.bootstrap.load_env", lambda *a, **k: None)
    monkeypatch.delenv("WALLET_DAILY_CAP_USD", raising=False)
    runner = CliRunner()
    result = runner.invoke(wallet_cmd, ["init", "--yes", "--home", str(tmp_path),
                                        "--data-dir", str(tmp_path / "w")])
    assert result.exit_code == 0, result.output
    assert "CEILING" in result.output
    assert "UNLIMITED" in result.output
    assert "set-cap daily" in result.output
