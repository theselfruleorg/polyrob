import hashlib
import json
from pathlib import Path

import pytest
from core.wallet import derivation


LEGACY_SEED = "x" * 40
# Standard BIP-39 test mnemonic (valid checksum, 12 words)
MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"


def test_legacy_derivation_matches_original_pbkdf2():
    key = derivation.derive_key(LEGACY_SEED, "treasury", "legacy")
    expected = hashlib.pbkdf2_hmac(
        "sha256", LEGACY_SEED.encode(), b"agent-wallet:treasury", 100_000, dklen=32)
    assert key == expected


def test_bip44_treasury_is_metamask_account_zero():
    # The whole point of bip44: account 0 of the mnemonic == treasury venue.
    eth_account = pytest.importorskip("eth_account")
    from eth_account import Account
    Account.enable_unaudited_hdwallet_features()
    expected = Account.from_mnemonic(MNEMONIC, account_path="m/44'/60'/0'/0/0")
    key = derivation.derive_key(MNEMONIC, "treasury", "bip44")
    assert key == bytes(expected.key)


def test_bip44_rejects_non_mnemonic_seed():
    pytest.importorskip("eth_account")
    with pytest.raises(ValueError):
        derivation.derive_key(LEGACY_SEED, "treasury", "bip44")


def test_unknown_scheme_and_venue_rejected():
    with pytest.raises(ValueError):
        derivation.derive_key(LEGACY_SEED, "treasury", "bip99")
    with pytest.raises(ValueError):
        derivation.derive_key(LEGACY_SEED, "nope", "legacy")


def test_resolve_scheme_absent_meta_is_legacy(tmp_path):
    assert derivation.resolve_scheme(env={}, data_dir=tmp_path) == "legacy"


def test_resolve_scheme_reads_meta_and_env_override_wins(tmp_path):
    derivation.write_scheme_once("bip44", data_dir=tmp_path)
    assert derivation.resolve_scheme(env={}, data_dir=tmp_path) == "bip44"
    assert derivation.resolve_scheme(
        env={"AGENT_WALLET_DERIVATION": "legacy"}, data_dir=tmp_path) == "legacy"


def test_resolve_scheme_raises_on_corrupt_existing_meta(tmp_path):
    """H2: a present-but-unreadable meta.json must FAIL FAST, not silently return
    'legacy' — a funded bip44 wallet would flip to a different address (PBKDF2
    happily digests a mnemonic string)."""
    (tmp_path / "meta.json").write_text("{ this is not json")
    with pytest.raises(ValueError):
        derivation.resolve_scheme(env={}, data_dir=tmp_path)


def test_resolve_scheme_raises_on_unknown_recorded_scheme(tmp_path):
    (tmp_path / "meta.json").write_text(json.dumps({"derivation": "wat"}))
    with pytest.raises(ValueError):
        derivation.resolve_scheme(env={}, data_dir=tmp_path)


def test_resolve_scheme_rejects_invalid_override(tmp_path):
    """An invalid AGENT_WALLET_DERIVATION must not be silently ignored (the
    operator believes they recovered, but nothing changed)."""
    with pytest.raises(ValueError):
        derivation.resolve_scheme(
            env={"AGENT_WALLET_DERIVATION": "bip-44"}, data_dir=tmp_path)


def test_h1_warns_legacy_scheme_with_mnemonic_seed_and_no_meta(tmp_path, caplog):
    """H1 (core-side minimum warning): scheme resolves 'legacy' AND the seed is a
    valid BIP-39 mnemonic AND no meta.json exists -> the wallet is silently deriving
    PBKDF2 addresses from a mnemonic string (the scheme-flip footgun that changes the
    funded treasury address). Emit a loud warning."""
    pytest.importorskip("eth_account")
    import logging
    caplog.set_level(logging.WARNING)
    msg = derivation.maybe_warn_legacy_mnemonic(MNEMONIC, "legacy", data_dir=tmp_path)
    assert msg is not None
    assert any(rec.levelno >= logging.WARNING and "bip44" in rec.message.lower()
               for rec in caplog.records)


def test_h1_no_warning_for_bip44_scheme(tmp_path):
    pytest.importorskip("eth_account")
    assert derivation.maybe_warn_legacy_mnemonic(MNEMONIC, "bip44", data_dir=tmp_path) is None


def test_h1_no_warning_for_legacy_non_mnemonic_seed(tmp_path):
    # A genuine legacy install's raw seed is not a mnemonic -> no false alarm.
    assert derivation.maybe_warn_legacy_mnemonic(LEGACY_SEED, "legacy", data_dir=tmp_path) is None


def test_h1_no_warning_when_meta_exists(tmp_path):
    """A meta.json present means the scheme was deliberately recorded — not the
    silent-flip footgun H1 targets."""
    pytest.importorskip("eth_account")
    derivation.write_scheme_once("legacy", data_dir=tmp_path)
    assert derivation.maybe_warn_legacy_mnemonic(MNEMONIC, "legacy", data_dir=tmp_path) is None


def test_write_scheme_once_refuses_conflicting_rewrite(tmp_path):
    derivation.write_scheme_once("bip44", data_dir=tmp_path)
    derivation.write_scheme_once("bip44", data_dir=tmp_path)  # idempotent OK
    with pytest.raises(ValueError):
        derivation.write_scheme_once("legacy", data_dir=tmp_path)


def test_wallet_meta_resolution_survives_cwd_change_via_data_dir_env(tmp_path, monkeypatch):
    """Finding 1 regression (2026-07-14 final review): the no-arg resolution used
    by `polyrob wallet init` (write) and the runtime (`resolve_scheme` read) must
    agree regardless of which directory each is invoked from. Before the fix,
    ``_wallet_data_dir`` resolved a bare CWD-relative ``./data/wallet`` — init from
    directory A and a later run from directory B would silently miss the meta file
    and flip bip44 -> legacy, deriving a DIFFERENT treasury address than the one
    the operator funded.
    """
    data_home = tmp_path / "datahome"
    monkeypatch.setenv("POLYROB_DATA_DIR", str(data_home))
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()

    monkeypatch.chdir(dir_a)
    derivation.write_scheme_once("bip44")  # no data_dir override -> anchored via POLYROB_DATA_DIR

    monkeypatch.chdir(dir_b)
    assert derivation.resolve_scheme(env={}) == "bip44"


def test_wallet_meta_path_and_audit_sink_share_directory_by_default(tmp_path, monkeypatch):
    """Finding 1 regression: the no-arg default paths of ``wallet_meta_path()``
    and ``default_audit_sink()`` must land in the SAME directory — otherwise
    ``write_scheme_once()`` and the audit sink would silently diverge."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "datahome2"))
    from core.wallet.audit_sink import default_audit_sink

    meta_path = derivation.wallet_meta_path()
    sink = default_audit_sink()
    assert meta_path.parent == Path(sink._path).parent


def test_agent_wallet_legacy_addresses_unchanged(tmp_path, monkeypatch):
    # Byte-identical guarantee for every existing install (no meta file).
    monkeypatch.setattr(derivation, "wallet_meta_path", lambda data_dir=None: tmp_path / "meta.json")
    from core.wallet.agent_wallet import AgentWallet
    from core.wallet.config import load_wallet_config
    cfg = load_wallet_config({"AGENT_WALLET_ENABLED": "true",
                              "AGENT_WALLET_MASTER_SEED": LEGACY_SEED})
    w = AgentWallet(cfg)
    expected_key = hashlib.pbkdf2_hmac(
        "sha256", LEGACY_SEED.encode(), b"agent-wallet:treasury", 100_000, dklen=32)
    from core.wallet.signer import LocalEoaSigner
    assert w.signer_for("treasury").address == LocalEoaSigner(expected_key).address
