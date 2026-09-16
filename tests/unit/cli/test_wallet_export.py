"""`polyrob wallet export` — TTY-gated, typed-confirmation reveal of the
wallet seed/mnemonic and per-venue private keys.

MONEY-CRITICAL: this is the ONLY place key material may ever be printed.
Refuses when stdin/stdout are not a TTY (piped output must never receive
keys), requires the literal typed confirmation "EXPORT", and is never an
agent-callable action (see test_no_agent_action_exposes_wallet_export).
"""
import os

import pytest
from click.testing import CliRunner

from cli.commands.wallet import wallet_cmd

SEED = "q" * 40


@pytest.fixture(autouse=True)
def _seeded(monkeypatch):
    # Never let the developer's real ~/.polyrob/.env leak a seed into these
    # tests (same load_env no-op pattern as test_wallet_init.py), and clean
    # AGENT_WALLET_* both before and after so nothing leaks across test files
    # in the same pytest session (run_wallet_init_flow-style env mutation
    # elsewhere in this suite doesn't apply here, but be consistent/defensive).
    monkeypatch.setattr("core.bootstrap.load_env", lambda *a, **k: None)
    monkeypatch.delenv("AGENT_WALLET_MASTER_SEED", raising=False)
    monkeypatch.delenv("AGENT_WALLET_ENABLED", raising=False)
    # A leaked AGENT_WALLET_DERIVATION=bip44 (H1 pin, set by init-flow tests that
    # mutate os.environ directly) would make this junk SEED derive → raise.
    monkeypatch.delenv("AGENT_WALLET_DERIVATION", raising=False)
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", SEED)
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    # Never let a real /etc/polyrob/*.env on the test host feed the fallback.
    monkeypatch.setattr("cli.commands.wallet._SYSTEM_ENV_FILES", ())
    yield
    os.environ.pop("AGENT_WALLET_MASTER_SEED", None)
    os.environ.pop("AGENT_WALLET_ENABLED", None)


def test_export_refuses_without_tty(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    runner = CliRunner()
    result = runner.invoke(wallet_cmd, ["export"])
    assert result.exit_code != 0
    assert "tty" in result.output.lower() or "interactive" in result.output.lower()
    assert SEED not in result.output


def _force_tty(monkeypatch):
    # click.testing.CliRunner.invoke() swaps sys.stdin/sys.stdout for brand
    # new _NamedTextIOWrapper instances INSIDE isolation() (see
    # click/testing.py), so a pre-invoke `monkeypatch.setattr("sys.stdin.isatty",
    # ...)` patches an object that gets replaced before the command ever runs —
    # the patch never lands on the wrapper the command actually sees. Patch the
    # wrapper CLASS instead so every instance CliRunner constructs (both stdin
    # and stdout) reports isatty()=True, regardless of identity.
    monkeypatch.setattr("click.testing._NamedTextIOWrapper.isatty", lambda self: True)


def test_export_requires_typed_confirmation(monkeypatch):
    _force_tty(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(wallet_cmd, ["export"], input="nope\n")
    assert result.exit_code != 0
    assert SEED not in result.output


def test_export_prints_key_after_confirmation(monkeypatch):
    _force_tty(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(wallet_cmd, ["export", "--venue", "treasury"], input="EXPORT\n")
    assert result.exit_code == 0, result.output
    assert "0x" in result.output           # a private key hex was printed
    assert "history" in result.output.lower() or "scrollback" in result.output.lower()


def test_export_without_seed_fails_cleanly(monkeypatch):
    monkeypatch.delenv("AGENT_WALLET_MASTER_SEED", raising=False)
    _force_tty(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(wallet_cmd, ["export"], input="EXPORT\n")
    assert result.exit_code != 0
    assert "wallet init" in result.output


def test_no_agent_action_exposes_wallet_export():
    # Invariant: export is CLI-only — never an agent-callable action.
    import pathlib
    src = pathlib.Path("tools/controller/action_registration.py").read_text()
    assert "wallet_export" not in src
    assert "export_wallet" not in src


def test_export_prints_solana_key_for_full_export(monkeypatch):
    """The same seed derives the Solana account; a full export must include it in
    the form Phantom imports (2026-09-14 — the owner could not move the SOL)."""
    pytest.importorskip("solders")
    from core.wallet.solana_signer import derive_solana_keypair
    _force_tty(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(wallet_cmd, ["export"], input="EXPORT\n")
    assert result.exit_code == 0, result.output
    kp = derive_solana_keypair(SEED, 0)
    assert str(kp.pubkey()) in result.output
    assert str(kp) in result.output              # base58 64-byte keypair
    # a venue-scoped export stays EVM-only
    result = runner.invoke(wallet_cmd, ["export", "--venue", "treasury"], input="EXPORT\n")
    assert str(kp) not in result.output


def test_export_reads_seed_from_system_env_files(monkeypatch, tmp_path):
    """On a systemd box the seed sits in /etc/polyrob/wallet.env, which the CLI
    does not load — export reads it itself (root-readable), wallet.env first."""
    monkeypatch.delenv("AGENT_WALLET_MASTER_SEED", raising=False)
    wallet_env = tmp_path / "wallet.env"
    wallet_env.write_text(f"AGENT_WALLET_MASTER_SEED=\"{SEED}\"\n")
    legacy = tmp_path / "polyrob.env"
    legacy.write_text("AGENT_WALLET_MASTER_SEED=" + "z" * 40 + "\n")
    monkeypatch.setattr("cli.commands.wallet._SYSTEM_ENV_FILES", (str(wallet_env), str(legacy)))
    _force_tty(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(wallet_cmd, ["export", "--venue", "treasury"], input="EXPORT\n")
    assert result.exit_code == 0, result.output
    from core.wallet import derivation
    key = derivation.derive_key(SEED, "treasury", "legacy")
    assert "0x" + key.hex() in result.output
