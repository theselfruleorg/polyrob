"""H3 (tamper-evident sink half) + L3 (data-dir resolution) for the wallet audit sink."""
import logging
import os

import pytest

from core.wallet.audit_sink import JsonlAuditSink, _wallet_data_dir


def _entry(i):
    return {"ts": 1000.0 + i, "venue": "x402", "amount_usd": 1.0,
            "idempotency_key": f"k{i}"}


def test_tamper_evident_warns_when_reloaded_shorter_than_hwm(tmp_path, caplog):
    """H3: if the reloaded JSONL is SHORTER than the last persisted high-water count,
    warn loudly. An injected agent truncating audit.jsonl (POLYROB_LOCAL) would
    otherwise silently reset the rolling-24h cap + payment-replay guard on the next
    restart."""
    path = str(tmp_path / "wallet" / "audit.jsonl")
    sink = JsonlAuditSink(path)
    for i in range(3):
        sink.append(_entry(i))          # persists high-water mark -> 3

    # simulate tamper: truncate the JSONL but leave the high-water mark behind
    with open(path, "w"):
        pass

    caplog.set_level(logging.ERROR)
    reloaded = JsonlAuditSink(path)     # loads 0 entries, HWM says 3
    assert len(reloaded) == 0
    assert any(
        rec.levelno >= logging.ERROR
        and any(w in rec.message.lower() for w in ("tamper", "shorter", "truncat"))
        for rec in caplog.records
    )


def test_no_warning_on_clean_reload(tmp_path, caplog):
    """A normal restart (JSONL intact) must NOT warn."""
    path = str(tmp_path / "wallet" / "audit.jsonl")
    sink = JsonlAuditSink(path)
    for i in range(3):
        sink.append(_entry(i))

    caplog.set_level(logging.ERROR)
    reloaded = JsonlAuditSink(path)     # loads 3, HWM 3 -> no warning
    assert len(reloaded) == 3
    assert not any(
        any(w in rec.message.lower() for w in ("tamper", "shorter", "truncat"))
        for rec in caplog.records
    )


def test_hwm_never_regresses_after_tamper(tmp_path, caplog):
    """After a detected truncation the high-water mark must not silently drop back to
    the tampered count — evidence persists across further restarts."""
    path = str(tmp_path / "wallet" / "audit.jsonl")
    s = JsonlAuditSink(path)
    for i in range(4):
        s.append(_entry(i))             # HWM -> 4
    with open(path, "w"):
        pass
    reloaded = JsonlAuditSink(path)     # HWM stays >= 4
    reloaded.append(_entry(99))         # one legit append after tamper
    caplog.set_level(logging.ERROR)
    again = JsonlAuditSink(path)        # loads 1 entry, HWM still >= 4 -> still warns
    assert len(again) == 1
    assert any(
        any(w in rec.message.lower() for w in ("tamper", "shorter", "truncat"))
        for rec in caplog.records
    )


def test_relative_data_dir_is_resolved(monkeypatch, tmp_path):
    """L3: a relative POLYROB_DATA_DIR must be resolved to an absolute path so it does
    not shift with the process CWD (which would split the meta/audit root and flip
    resolve_scheme to legacy)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "reldata").mkdir()
    monkeypatch.setenv("POLYROB_DATA_DIR", "reldata")
    resolved = _wallet_data_dir()
    assert os.path.isabs(resolved)
    assert resolved == os.path.join(str((tmp_path / "reldata").resolve()), "wallet")


def test_meta_read_fails_closed_when_data_home_unresolvable(monkeypatch):
    """L3: with POLYROB_DATA_DIR unset AND resolve_data_home() raising, a META read
    must NOT silently fall back to a CWD-relative ./data/wallet — that risks a silent
    derivation-scheme flip (wrong funded address). Fail closed for meta; the audit
    path stays fail-open (losing audit resets caps, never blocks a live spend)."""
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    import core.runtime_paths as rp

    def _boom():
        raise RuntimeError("data-home unresolvable")

    monkeypatch.setattr(rp, "resolve_data_home", _boom)

    with pytest.raises(Exception):
        _wallet_data_dir(for_meta=True)

    # audit (non-meta) still fails open to the legacy ./data path
    assert _wallet_data_dir(for_meta=False).endswith(os.path.join("data", "wallet"))
