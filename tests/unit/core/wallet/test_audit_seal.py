"""WAL-9: the wallet audit ledger is sealed with a key derived from the master
seed, so a same-UID process that edits a row (the amount, the time) while it
keeps the line count no longer resets the rolling cap at the next start."""
import json
import logging

from core.wallet.audit_sink import JsonlAuditSink

KEY = b"k" * 32


def _entry(i, amount=10.0):
    return {"ts": 1000.0 + i, "venue": "evm", "amount_usd": amount,
            "idempotency_key": f"k{i}"}


def _sink(tmp_path, key=KEY):
    return JsonlAuditSink(str(tmp_path / "wallet" / "audit.jsonl"), seal_key=key)


def test_clean_reload_is_healthy_and_sealed(tmp_path):
    s = _sink(tmp_path)
    for i in range(3):
        s.append(_entry(i))
    seal = json.loads((tmp_path / "wallet" / "audit.jsonl.seal").read_text())
    assert seal["n"] == 3
    again = _sink(tmp_path)
    assert again.healthy and len(again) == 3


def test_edited_row_with_same_line_count_fails_closed(tmp_path, caplog):
    s = _sink(tmp_path)
    for i in range(3):
        s.append(_entry(i, amount=500.0))
    path = tmp_path / "wallet" / "audit.jsonl"
    path.write_text(path.read_text().replace('"amount_usd": 500.0', '"amount_usd": 0.0'))
    caplog.set_level(logging.ERROR)
    again = _sink(tmp_path)
    assert len(again) == 3
    assert again.healthy is False
    assert any("seal" in r.message for r in caplog.records)


def test_a_wrong_key_cannot_forge_the_seal(tmp_path):
    s = _sink(tmp_path, key=b"x" * 32)          # an attacker re-seals with its own key
    s.append(_entry(0))
    assert _sink(tmp_path).healthy is False


def test_legacy_unsealed_ledger_is_sealed_once_not_refused(tmp_path, caplog):
    legacy = _sink(tmp_path, key=None)         # pre-seal rows, no key
    for i in range(2):
        legacy.append(_entry(i))
    assert not (tmp_path / "wallet" / "audit.jsonl.seal").exists()
    caplog.set_level(logging.WARNING)
    keyed = _sink(tmp_path)
    assert keyed.healthy and len(keyed) == 2
    assert (tmp_path / "wallet" / "audit.jsonl.seal").exists()
    assert _sink(tmp_path).healthy


def test_seedless_writer_tail_is_accepted_and_resealed(tmp_path):
    keyed = _sink(tmp_path)
    keyed.append(_entry(0))
    seedless = _sink(tmp_path, key=None)       # e.g. a CLI release with no seed
    seedless.append(_entry(1))
    again = _sink(tmp_path)
    assert again.healthy and len(again) == 2
    assert json.loads((tmp_path / "wallet" / "audit.jsonl.seal").read_text())["n"] == 2


def test_cross_process_refresh_keeps_the_chain(tmp_path):
    a = _sink(tmp_path)
    b = _sink(tmp_path)
    a.append(_entry(0))
    b.append(_entry(1))                         # b refreshes a's row first
    a.append(_entry(2))
    assert _sink(tmp_path).healthy


def test_no_key_means_no_seal_and_no_verification(tmp_path):
    s = _sink(tmp_path, key=None)
    s.append(_entry(0))
    assert not (tmp_path / "wallet" / "audit.jsonl.seal").exists()
    assert _sink(tmp_path, key=None).healthy
