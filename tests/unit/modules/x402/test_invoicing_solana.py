"""svm (Solana) invoice creation — recipient honesty + no jitter (2026-08-26).

Three creation-side properties the Phase 4 wiring stamped (`chain_family`,
`solana_reference`) but did not finish:

1. An svm invoice's ``recipient`` must be the wallet's OWN Solana address —
   ``X402_PAYMENT_RECIPIENT`` is an EVM treasury, and "pay this 0x… address"
   on a Solana invoice is instructions a payer cannot follow.
2. base58 is case-SENSITIVE: the legacy ``recipient.lower()`` normalization
   destroys a Solana address (it decodes to different bytes, or nothing).
   Lowercasing is an EVM-hex convenience only.
3. Amount-jitter must NOT apply to svm rows: matching there is by the unique
   Solana Pay reference, never by amount (see `_scan_solana`), so jitter is
   pure amount noise the payer has to reproduce exactly. The handoff is
   explicit: do not carry the EVM matching strategy across.
"""
import pytest

from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles
from modules.database.x402_tables import X402Tables
from modules.x402 import invoicing

SOLANA_TREASURY = "BrsPwATRZcb2PWsEZba9Bh1mwcxU6M7R64nPgRneCpmL"


async def _setup_db(tmp_path):
    db = DatabaseConnection(tmp_path / "x402.db")
    await db.connect()
    await UserProfiles(db).create_table()
    await X402Tables(db).create_tables()
    return db


@pytest.fixture(autouse=True)
def _svm_env(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0xEVMTREASURY")
    monkeypatch.delenv("X402_DEFAULT_CHAIN", raising=False)
    for var in ("X402_INVOICE_MAX_USD", "X402_INVOICE_DAILY_MAX",
                "X402_SETTLE_ONCHAIN_DETECT", "X402_INVOICE_AMOUNT_JITTER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(invoicing, "_svm_treasury", lambda: SOLANA_TREASURY)


@pytest.mark.asyncio
async def test_svm_invoice_pays_to_the_solana_treasury_case_preserved(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=0.10,
            purpose="phase 4 round trip", chain="solana", db=db)
        row = await db.fetch_one(
            "SELECT recipient, chain, metadata FROM x402_payment_requests WHERE id = ?",
            (inv["request_id"],))
        # byte-for-byte: NOT lowercased, NOT the EVM treasury
        assert row["recipient"] == SOLANA_TREASURY
        assert row["chain"] == "solana"
        import json
        raw_meta = row["metadata"]
        meta = json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta
        assert meta["chain_family"] == "svm"
        assert meta["solana_reference"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_svm_invoice_refuses_without_a_solana_treasury(tmp_path, monkeypatch):
    """Falling back to the EVM 0x… treasury would print an address a Solana
    payer cannot pay — refuse with a reason instead."""
    monkeypatch.setattr(invoicing, "_svm_treasury", lambda: None)
    db = await _setup_db(tmp_path)
    try:
        with pytest.raises(ValueError, match="[Ss]olana"):
            await invoicing.create_payment_request(
                user_id="rob", session_id="s1", amount_usd=0.10,
                purpose="phase 4 round trip", chain="solana", db=db)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_svm_amounts_are_never_jittered(tmp_path, monkeypatch):
    """Same-amount collision with detection ON — the exact EVM jitter trigger —
    must leave both svm amounts untouched: svm settlement matches by reference,
    never by amount."""
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    monkeypatch.setenv("X402_INVOICE_AMOUNT_JITTER", "true")
    db = await _setup_db(tmp_path)
    try:
        first = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=0.10, purpose="a",
            chain="solana", db=db)
        second = await invoicing.create_payment_request(
            user_id="rob", session_id="s2", amount_usd=0.10, purpose="b",
            chain="solana", db=db)
        assert first["amount_usd"] == 0.10
        assert second["amount_usd"] == 0.10
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_evm_recipient_is_still_lowercased(tmp_path, monkeypatch):
    """The EVM normalization is deliberately unchanged — hex is case-insensitive
    and the EVM scan matches rows by lowercased treasury."""
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0xAbCdTREASURY")
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=1.0, purpose="a", db=db)
        row = await db.fetch_one(
            "SELECT recipient FROM x402_payment_requests WHERE id = ?",
            (inv["request_id"],))
        assert row["recipient"] == "0xabcdtreasury"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_the_env_default_path_is_unchanged(tmp_path, monkeypatch):
    """Every existing caller passes no `chain`, so the config value must still
    decide — the new parameter widens the API without moving anyone."""
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=0.10,
            purpose="evm default", db=db)
        row = await db.fetch_one(
            "SELECT chain, recipient FROM x402_payment_requests WHERE id = ?",
            (inv["request_id"],))
        assert row["chain"] == "base"
        # EVM recipients stay lowercased — hex folding is the convenience the
        # normalizer keeps for exactly this family.
        assert row["recipient"] == row["recipient"].lower()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_an_explicit_chain_beats_the_env_default(tmp_path, monkeypatch):
    """The chain is a property of the INVOICE. Relying on process-wide env to
    select it is what made Solana invoices unreachable, and it would silently
    retarget any EVM invoice created alongside one."""
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=0.10,
            purpose="explicit svm", chain="solana", db=db)
        row = await db.fetch_one(
            "SELECT chain, recipient FROM x402_payment_requests WHERE id = ?",
            (inv["request_id"],))
        assert row["chain"] == "solana"
        assert row["recipient"] == SOLANA_TREASURY
    finally:
        await db.close()


# -- _norm_tx: a Solana signature is base58, case-significant ----------------

def test_norm_tx_preserves_a_base58_solana_signature():
    """Lowercasing a base58 signature destroys provenance: the stored value
    resolves on no explorer and matches no getTransaction. Fold only 0x-hex."""
    sig = ("4gesN42KaXbUqPhaLXiCFouNhkkcQvUZTS7XT7VaXhJTPYii5i"
           "AbCdEfGhJkMnPqRsTuVwXyZ123456789")
    assert invoicing._norm_tx(sig) == sig


def test_norm_tx_still_folds_an_evm_hash():
    h = "0xAbC123" + "0" * 58
    assert invoicing._norm_tx(h) == h.lower()


def test_norm_tx_replay_guard_is_total_either_way():
    """Store and compare fold identically, so replay detection is unchanged."""
    sig = "5KtP9vBase58SigCASEsensitiveXyZ"
    assert invoicing._norm_tx(sig) == invoicing._norm_tx(sig)
    h = "0xDEADbeef" + "0" * 56
    assert invoicing._norm_tx(h) == invoicing._norm_tx(h.upper().replace("0X", "0x"))
