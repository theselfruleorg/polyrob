"""Task 15 (Phase 4): ReputationManager.submit_feedback verifies a submitted
ProofOfPayment against the REAL x402 settlement ledger before accepting it as
a "verified-purchase" signal — a sybil must not be able to fabricate a proof
by just filling in plausible-looking fields. Feedback WITHOUT a proof is the
existing (unverified) path and must be unaffected.
"""
import pytest

from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles
from modules.database.x402_tables import X402Tables
from modules.eip8004.models import ProofOfPayment
from modules.eip8004.reputation import ReputationManager
from modules.x402 import invoicing

_KEY = "0x" + "0" * 63 + "1"
_AGENT_WALLET = "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"  # address of _KEY
_CLIENT = "0x" + "2" * 40


@pytest.fixture(autouse=True)
def _treasury_env(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0xTREASURY")


async def _setup_db(tmp_path):
    db = DatabaseConnection(tmp_path / "x402.db")
    await db.connect()
    await UserProfiles(db).create_table()
    await X402Tables(db).create_tables()
    return db


def _mgr(monkeypatch, db):
    monkeypatch.setenv("EIP8004_AGENT_ID", "42")
    monkeypatch.setenv("EIP8004_AGENT_WALLET", _AGENT_WALLET)
    monkeypatch.setenv("EIP8004_CHAIN_ID", "8453")
    monkeypatch.setenv("EIP8004_AGENT_PRIVATE_KEY", _KEY)
    return ReputationManager(db=db)


@pytest.mark.asyncio
async def test_proof_referencing_settled_invoice_is_verified(tmp_path, monkeypatch):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=4.0, purpose="consulting", db=db)
        await invoicing.settle_payment_request(
            inv["request_id"], transaction_hash="0xfeed", db=db)

        mgr = _mgr(monkeypatch, db)
        auth = await mgr.create_feedback_auth(client_address=_CLIENT)
        proof = ProofOfPayment(
            fromAddress="0xPAYER", toAddress="0xTREASURY",
            chainId="8453", txHash="0xfeed")

        result = await mgr.submit_feedback(
            agent_id=42, score=90, feedback_auth=auth, proof_of_payment=proof)
        assert result["success"] is True
        assert result["verified_purchase"] is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_proof_referencing_nonexistent_invoice_rejected(tmp_path, monkeypatch):
    db = await _setup_db(tmp_path)
    try:
        mgr = _mgr(monkeypatch, db)
        auth = await mgr.create_feedback_auth(client_address=_CLIENT)
        proof = ProofOfPayment(
            fromAddress="0xPAYER", toAddress="0xTREASURY",
            chainId="8453", txHash="0xNOSUCHTX")

        with pytest.raises(ValueError):
            await mgr.submit_feedback(
                agent_id=42, score=90, feedback_auth=auth, proof_of_payment=proof)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_proof_referencing_pending_unsettled_invoice_rejected(tmp_path, monkeypatch):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=4.0, purpose="consulting", db=db)
        # NEVER settled — still pending. A sybil claiming a tx hash that was
        # never actually recorded as a settlement must be rejected even
        # though the request_id itself is real.
        mgr = _mgr(monkeypatch, db)
        auth = await mgr.create_feedback_auth(client_address=_CLIENT)
        proof = ProofOfPayment(
            fromAddress="0xPAYER", toAddress="0xTREASURY",
            chainId="8453", txHash="0xNEVERSETTLED")

        with pytest.raises(ValueError):
            await mgr.submit_feedback(
                agent_id=42, score=90, feedback_auth=auth, proof_of_payment=proof)
        # sanity: the invoice really is still pending, not settled
        row = await invoicing.get_payment_request(inv["request_id"], db=db)
        assert row["status"] == "pending"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_submit_feedback_without_proof_is_unaffected(tmp_path, monkeypatch):
    """The existing unverified path must keep working exactly as before —
    no proof means no verification attempted, no new rejection."""
    db = await _setup_db(tmp_path)
    try:
        mgr = _mgr(monkeypatch, db)
        auth = await mgr.create_feedback_auth(client_address=_CLIENT)
        result = await mgr.submit_feedback(agent_id=42, score=75, feedback_auth=auth)
        assert result["success"] is True
        assert "verified_purchase" not in result
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Follow-up hardening (Task 15 review, Finding 1): one-proof-one-feedback
# replay guard — the SAME settled tx hash must not back unlimited feedback
# submissions.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_replayed_proof_is_rejected_on_second_use(tmp_path, monkeypatch):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=4.0, purpose="consulting", db=db)
        await invoicing.settle_payment_request(
            inv["request_id"], transaction_hash="0xreplay", db=db)

        mgr = _mgr(monkeypatch, db)
        auth = await mgr.create_feedback_auth(client_address=_CLIENT)
        proof = ProofOfPayment(
            fromAddress="0xPAYER", toAddress="0xTREASURY",
            chainId="8453", txHash="0xreplay")

        first = await mgr.submit_feedback(
            agent_id=42, score=90, feedback_auth=auth, proof_of_payment=proof)
        assert first["verified_purchase"] is True

        # Same tx hash, second attempt — must be rejected even though the
        # underlying invoice is genuinely settled (that's exactly the point:
        # a real settled payment is not an unlimited feedback token).
        with pytest.raises(ValueError, match="already used"):
            await mgr.submit_feedback(
                agent_id=42, score=90, feedback_auth=auth, proof_of_payment=proof)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_different_settled_tx_is_still_accepted_after_a_replay_rejection(tmp_path, monkeypatch):
    db = await _setup_db(tmp_path)
    try:
        inv_x = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=4.0, purpose="consulting", db=db)
        await invoicing.settle_payment_request(
            inv_x["request_id"], transaction_hash="0xtxX", db=db)
        inv_y = await invoicing.create_payment_request(
            user_id="rob", session_id="s2", amount_usd=4.0, purpose="consulting", db=db)
        await invoicing.settle_payment_request(
            inv_y["request_id"], transaction_hash="0xtxY", db=db)

        mgr = _mgr(monkeypatch, db)
        auth = await mgr.create_feedback_auth(client_address=_CLIENT)

        proof_x = ProofOfPayment(
            fromAddress="0xPAYER", toAddress="0xTREASURY", chainId="8453", txHash="0xtxX")
        result_x = await mgr.submit_feedback(
            agent_id=42, score=80, feedback_auth=auth, proof_of_payment=proof_x)
        assert result_x["verified_purchase"] is True

        with pytest.raises(ValueError, match="already used"):
            await mgr.submit_feedback(
                agent_id=42, score=80, feedback_auth=auth, proof_of_payment=proof_x)

        # A DIFFERENT settled tx must still be accepted — the guard is
        # per-proof, not "one verified-purchase feedback ever".
        proof_y = ProofOfPayment(
            fromAddress="0xPAYER2", toAddress="0xTREASURY", chainId="8453", txHash="0xtxY")
        result_y = await mgr.submit_feedback(
            agent_id=42, score=85, feedback_auth=auth, proof_of_payment=proof_y)
        assert result_y["verified_purchase"] is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_replay_guard_rejects_varied_agent_id_bypass(tmp_path, monkeypatch):
    """Regression test for the REPRODUCED bypass in fix pass 1: the replay
    guard used to key on ``(agentId, txHash)``, and ``agent_id`` is
    caller-controlled request-body input that was never validated against
    the configured/authorized agent. A client could submit the SAME real
    settled txHash + the SAME signed `feedback_auth`, varying `agent_id`
    (42, 7, 999, ...) on every call — each landing in a different guard
    bucket — to mint unlimited `verified_purchase` feedback off ONE payment.

    Fix pass 2 closes this two ways, either sufficient alone: (1) the replay
    guard now keys on ``txHash`` ALONE, so a second submission of the same
    tx is rejected regardless of what `agent_id` claims; (2) `agent_id` is
    now bound to the configured/authorized agent, so a varied `agent_id`
    is rejected before the replay guard is even consulted.
    """
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=4.0, purpose="consulting", db=db)
        await invoicing.settle_payment_request(
            inv["request_id"], transaction_hash="0xshared", db=db)

        mgr = _mgr(monkeypatch, db)
        auth = await mgr.create_feedback_auth(client_address=_CLIENT)
        proof = ProofOfPayment(
            fromAddress="0xPAYER", toAddress="0xTREASURY",
            chainId="8453", txHash="0xshared")

        result_agent_42 = await mgr.submit_feedback(
            agent_id=42, score=90, feedback_auth=auth, proof_of_payment=proof)
        assert result_agent_42["verified_purchase"] is True

        # SAME agent, SAME tx — plain replay, must be rejected.
        with pytest.raises(ValueError, match="already used"):
            await mgr.submit_feedback(
                agent_id=42, score=90, feedback_auth=auth, proof_of_payment=proof)

        # THE BYPASS: same real settled tx + same signed auth, but a
        # DIFFERENT (unauthorized) agent_id. This must ALSO be rejected —
        # it must not mint a second verified_purchase entry off one payment.
        for bogus_agent_id in (7, 999):
            with pytest.raises(ValueError):
                await mgr.submit_feedback(
                    agent_id=bogus_agent_id, score=90, feedback_auth=auth,
                    proof_of_payment=proof)

        # The feedback cache must never have accumulated a second entry for
        # this tx under any agent bucket.
        assert len(mgr._feedback_cache.get(42, [])) == 1
        assert 7 not in mgr._feedback_cache
        assert 999 not in mgr._feedback_cache
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_submit_feedback_rejects_agent_id_not_matching_configured_agent(
    tmp_path, monkeypatch
):
    """`agent_id` is caller-controlled request-body input — it must be bound
    to the configured `EIP8004_AGENT_ID` (or the authorized
    `feedback_auth.agentId`), never trusted as an arbitrary label. This is
    the identity-binding half of the fix pass 2 regression: even WITHOUT a
    payment proof, a client must not be able to misattribute real feedback
    into an arbitrary agent's `_feedback_cache` bucket.
    """
    db = await _setup_db(tmp_path)
    try:
        mgr = _mgr(monkeypatch, db)  # EIP8004_AGENT_ID=42
        auth = await mgr.create_feedback_auth(client_address=_CLIENT)

        # feedback_auth.agentId is 42 (from create_feedback_auth); submitting
        # with agent_id=7 doesn't match the configured agent OR the
        # authorized agentId — reject.
        with pytest.raises(ValueError):
            await mgr.submit_feedback(agent_id=7, score=90, feedback_auth=auth)

        # Sanity: the matching agent_id still works.
        result = await mgr.submit_feedback(agent_id=42, score=90, feedback_auth=auth)
        assert result["success"] is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_rejected_submission_does_not_consume_the_proof(tmp_path, monkeypatch):
    """A submission that fails for an UNRELATED reason (bad score) must not
    burn the payer's one legitimate use of their proof."""
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=4.0, purpose="consulting", db=db)
        await invoicing.settle_payment_request(
            inv["request_id"], transaction_hash="0xretry", db=db)

        mgr = _mgr(monkeypatch, db)
        auth = await mgr.create_feedback_auth(client_address=_CLIENT)
        proof = ProofOfPayment(
            fromAddress="0xPAYER", toAddress="0xTREASURY",
            chainId="8453", txHash="0xretry")

        # Bad score fails BEFORE the proof is even looked at (auth/score
        # validation runs first) — the proof must still be usable after.
        with pytest.raises(ValueError):
            await mgr.submit_feedback(
                agent_id=42, score=999, feedback_auth=auth, proof_of_payment=proof)

        result = await mgr.submit_feedback(
            agent_id=42, score=90, feedback_auth=auth, proof_of_payment=proof)
        assert result["verified_purchase"] is True
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Follow-up hardening (Task 15 review, Finding 2): the proof's `toAddress`
# must match the settled invoice's own `recipient` — a proof referencing a
# real settled payment that reached SOMEONE ELSE'S treasury must not count.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_proof_toaddress_mismatch_rejected(tmp_path, monkeypatch):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=4.0, purpose="consulting", db=db)
        await invoicing.settle_payment_request(
            inv["request_id"], transaction_hash="0xwrongto", db=db)

        mgr = _mgr(monkeypatch, db)
        auth = await mgr.create_feedback_auth(client_address=_CLIENT)
        # This tx really did settle — but the proof claims it paid a
        # DIFFERENT treasury than the invoice's actual recipient.
        proof = ProofOfPayment(
            fromAddress="0xPAYER", toAddress="0xSOMEONE_ELSE",
            chainId="8453", txHash="0xwrongto")

        with pytest.raises(ValueError):
            await mgr.submit_feedback(
                agent_id=42, score=90, feedback_auth=auth, proof_of_payment=proof)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_proof_toaddress_case_insensitive_match_accepted(tmp_path, monkeypatch):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=4.0, purpose="consulting", db=db)
        await invoicing.settle_payment_request(
            inv["request_id"], transaction_hash="0xcasetx", db=db)

        mgr = _mgr(monkeypatch, db)
        auth = await mgr.create_feedback_auth(client_address=_CLIENT)
        # Invoice recipient is stored lower-cased ("0xtreasury"); the proof
        # supplies a differently-cased but equivalent address.
        proof = ProofOfPayment(
            fromAddress="0xPAYER", toAddress="0xTrEaSuRy",
            chainId="8453", txHash="0xcasetx")

        result = await mgr.submit_feedback(
            agent_id=42, score=90, feedback_auth=auth, proof_of_payment=proof)
        assert result["verified_purchase"] is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_proof_missing_toaddress_rejected(tmp_path, monkeypatch):
    """Strict policy: `proof_from_settled_invoice` (the only proof-building
    path today) always populates `toAddress` from the settled invoice's own
    recipient, so an empty `toAddress` on a submitted proof is treated as
    unverifiable rather than silently skipping the treasury-match check."""
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=4.0, purpose="consulting", db=db)
        await invoicing.settle_payment_request(
            inv["request_id"], transaction_hash="0xnoaddr", db=db)

        mgr = _mgr(monkeypatch, db)
        auth = await mgr.create_feedback_auth(client_address=_CLIENT)
        proof = ProofOfPayment(
            fromAddress="0xPAYER", toAddress="",
            chainId="8453", txHash="0xnoaddr")

        with pytest.raises(ValueError):
            await mgr.submit_feedback(
                agent_id=42, score=90, feedback_auth=auth, proof_of_payment=proof)
    finally:
        await db.close()
