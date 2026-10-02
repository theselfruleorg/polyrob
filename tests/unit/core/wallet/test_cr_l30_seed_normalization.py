"""CR-L30: the master seed is normalized ONCE, and never moves a live address."""
import json

import pytest

from core.wallet import derivation
from core.wallet.config import load_wallet_config, normalize_master_seed

LEGACY = "x" * 40
MNEMONIC = ("abandon abandon abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon about")


@pytest.fixture
def legacy_home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AGENT_WALLET_DERIVATION", raising=False)
    return tmp_path


@pytest.fixture
def bip44_home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AGENT_WALLET_DERIVATION", raising=False)
    meta = derivation.wallet_meta_path()
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps({"derivation": "bip44"}))
    return tmp_path


def test_clean_legacy_seed_is_unchanged(legacy_home):
    cfg = load_wallet_config({"AGENT_WALLET_ENABLED": "true",
                              "AGENT_WALLET_MASTER_SEED": LEGACY})
    assert cfg.master_seed == LEGACY
    assert normalize_master_seed(LEGACY) == LEGACY


def test_padded_legacy_seed_is_refused(legacy_home):
    with pytest.raises(ValueError, match="whitespace"):
        load_wallet_config({"AGENT_WALLET_ENABLED": "true",
                            "AGENT_WALLET_MASTER_SEED": LEGACY + " "})


def test_padded_bip44_seed_is_stripped_and_address_is_stable(bip44_home):
    pytest.importorskip("eth_account")
    cfg = load_wallet_config({"AGENT_WALLET_ENABLED": "true",
                              "AGENT_WALLET_MASTER_SEED": f" {MNEMONIC} "})
    assert cfg.master_seed == MNEMONIC
    assert derivation.derive_key(cfg.master_seed, "treasury", "bip44") == \
        derivation.derive_key(f" {MNEMONIC} ", "treasury", "bip44")


def test_disabled_wallet_passes_seed_through(legacy_home):
    cfg = load_wallet_config({"AGENT_WALLET_MASTER_SEED": LEGACY + " "})
    assert cfg.master_seed == LEGACY + " "


def test_doctor_reports_a_padded_legacy_seed_as_misconfigured(legacy_home):
    from cli.commands.doctor import doctor_report
    lines = doctor_report({"AGENT_WALLET_ENABLED": "true",
                           "AGENT_WALLET_MASTER_SEED": LEGACY + " ",
                           "POLYROB_DATA_DIR": str(legacy_home)})
    assert any("MISCONFIGURED" in l and "whitespace" in l for l in lines), lines
