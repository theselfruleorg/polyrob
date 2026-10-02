"""CR-L31: deleting the ledger AND its high-water mark must not reset the caps."""
import json
import os

from core.wallet import derivation
from core.wallet.audit_sink import JsonlAuditSink


def _sink(tmp_path):
    return JsonlAuditSink(str(tmp_path / "wallet" / "audit.jsonl"))


def test_fresh_home_is_healthy_and_creates_nothing(tmp_path):
    s = _sink(tmp_path)
    assert s.healthy
    assert not (tmp_path / "wallet").exists()


def test_missing_ledger_and_mark_with_a_wallet_of_record_is_unhealthy(tmp_path):
    w = tmp_path / "wallet"
    w.mkdir()
    (w / "public_identity.json").write_text(json.dumps({"evm": {"treasury": "0x1"}}))
    assert _sink(tmp_path).healthy is False


def test_an_empty_ledger_confirms_a_never_spent_wallet(tmp_path):
    w = tmp_path / "wallet"
    w.mkdir()
    (w / "meta.json").write_text(json.dumps({"derivation": "bip44"}))
    (w / "audit.jsonl").write_text("")
    assert _sink(tmp_path).healthy


def test_genesis_then_delete_both_is_detected(tmp_path):
    s = _sink(tmp_path)
    s.ensure_genesis()
    w = tmp_path / "wallet"
    assert (w / "audit.jsonl.hwm").read_text() == "0"
    (w / "public_identity.json").write_text("{}")
    assert _sink(tmp_path).healthy  # genesis mark present: a fresh wallet
    os.unlink(w / "audit.jsonl.hwm")
    assert _sink(tmp_path).healthy is False


def test_write_scheme_once_plants_genesis_before_the_meta(tmp_path):
    derivation.write_scheme_once("bip44", data_dir=tmp_path / "wallet")
    assert (tmp_path / "wallet" / "audit.jsonl.hwm").is_file()
    assert _sink(tmp_path).healthy


def test_seeded_agent_wallet_plants_genesis(tmp_path, monkeypatch):
    from core.wallet.agent_wallet import AgentWallet
    from core.wallet.config import load_wallet_config
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AGENT_WALLET_DERIVATION", raising=False)
    cfg = load_wallet_config({"AGENT_WALLET_ENABLED": "true",
                              "AGENT_WALLET_MASTER_SEED": "x" * 40})
    AgentWallet(cfg, audit_sink=_sink(tmp_path))
    assert (tmp_path / "wallet" / "audit.jsonl.hwm").is_file()
