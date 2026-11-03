"""098 W1 MO-02 / MO-23: the x402 invoice machine.

A scan pass never claims (pending -> completed is ONE CAS); a `/pay` claim that
never reached the facilitator has a reaper; a submission is never released on
age; a legacy `settling` row and a stalled submission reach the owner ONCE.
"""
import asyncio
import json
import time

import pytest

from core.state.machine import IllegalTransition
from modules.x402 import invoicing, invoice_reaper
from modules.x402.invoice_status import X402_INVOICE
from modules.x402.settlement_watcher import SettlementWatcher

TREASURY = "0xtreasuryaddress000000000000000000000001"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", TREASURY)
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    for var in ("X402_INVOICE_MAX_USD", "X402_INVOICE_DAILY_MAX",
                "X402_SETTLE_ONCHAIN_DETECT", "X402_INVOICE_AMOUNT_JITTER"):
        monkeypatch.delenv(var, raising=False)


class _Agent:
    container = None

    async def deliver_self_wake(self, *a, **k):
        return True


async def _row(db, rid):
    row = await db.fetch_one("SELECT * FROM x402_payment_requests WHERE id=?", (rid,))
    meta = row["metadata"]
    return dict(row), (meta if isinstance(meta, dict) else json.loads(meta or "{}"))


async def _invoice(db, usd=5.0):
    inv = await invoicing.create_payment_request(
        user_id="alice", session_id="s1", amount_usd=usd, purpose="p", db=db)
    return inv["request_id"]


def _spy_settling(db, seen):
    real = db.execute

    async def execute(sql, params=()):
        if "UPDATE x402_payment_requests" in sql and "status=?" in sql.replace(" ", "") \
                and "settling" in tuple(params):
            seen.append(params)
        return await real(sql, params)
    db.execute = execute


@pytest.mark.asyncio
async def test_evm_scan_settles_pending_without_claim(x402_db):
    rid = await _invoice(x402_db)
    seen = []
    _spy_settling(x402_db, seen)
    watcher = SettlementWatcher(_Agent(), db=x402_db)
    transfer = {"tx_hash": "0x" + "ab" * 32, "from": "0xpayer", "amount_usd": 5.0, "block": 1}
    settled, unmatched = await watcher._settle_or_flag([transfer], TREASURY)
    assert (settled, unmatched) == (1, 0)
    assert (await _row(x402_db, rid))[0]["status"] == "completed"
    assert seen == []  # no `settling` write was ever issued


@pytest.mark.asyncio
async def test_evm_scan_cancel_leaves_invoice_pending_and_rescan_settles(x402_db, monkeypatch):
    rid = await _invoice(x402_db)
    watcher = SettlementWatcher(_Agent(), db=x402_db)
    notices = []

    async def unmatched(transfer, treasury):
        notices.append(transfer)
    watcher._notify_unmatched = unmatched
    real = invoicing.settle_payment_request

    async def cancelled(*a, **k):
        raise asyncio.CancelledError()
    monkeypatch.setattr(invoicing, "settle_payment_request", cancelled)
    transfer = {"tx_hash": "0x" + "cd" * 32, "from": "0xpayer", "amount_usd": 5.0, "block": 1}
    with pytest.raises(asyncio.CancelledError):
        await watcher._settle_or_flag([transfer], TREASURY)
    assert (await _row(x402_db, rid))[0]["status"] == "pending"
    monkeypatch.setattr(invoicing, "settle_payment_request", real)
    assert await watcher._settle_or_flag([transfer], TREASURY) == (1, 0)
    assert (await _row(x402_db, rid))[0]["status"] == "completed"
    assert notices == []


@pytest.mark.asyncio
async def test_scan_settle_never_completes_a_pay_claim(x402_db):
    """A row `/pay` claimed between the scan's match and its settle stays the
    facilitator's: the scan's CAS is from `pending` only."""
    rid = await _invoice(x402_db)
    assert await invoicing.claim_for_settlement(rid, db=x402_db)
    assert not await invoicing.settle_payment_request(
        rid, transaction_hash="0x" + "ef" * 32, db=x402_db, from_states=("pending",))
    assert (await _row(x402_db, rid))[0]["status"] == "settling"


@pytest.mark.asyncio
async def test_claim_stamps_lane_and_time(x402_db):
    rid = await _invoice(x402_db)
    assert await invoicing.claim_for_settlement(rid, db=x402_db)
    row, _ = await _row(x402_db, rid)
    assert row["claim_lane"] == "facilitator" and row["claimed_at"] > 0
    assert not await invoicing.claim_for_settlement(rid, db=x402_db)


@pytest.mark.asyncio
async def test_reaper_releases_unsubmitted_facilitator_claim_after_600s(x402_db):
    rid = await _invoice(x402_db)
    assert await invoicing.claim_for_settlement(rid, db=x402_db)
    assert await invoicing.revert_stale_settling(db=x402_db) == []  # too young
    out = await invoice_reaper.reap_unsubmitted_claims(db=x402_db, now=time.time() + 601)
    assert [r["request_id"] for r in out] == [rid]
    row, _ = await _row(x402_db, rid)
    assert row["status"] == "pending" and row["claim_lane"] is None


@pytest.mark.asyncio
async def test_reaper_never_releases_submitted_claim(x402_db):
    rid = await _invoice(x402_db)
    assert await invoicing.claim_for_settlement(rid, db=x402_db)
    await x402_db.execute(
        "UPDATE x402_payment_requests SET metadata=json_set(metadata,'$.facilitator_submitted',1) "
        "WHERE id=?", (rid,))
    assert await invoice_reaper.reap_unsubmitted_claims(
        db=x402_db, now=time.time() + 86400) == []
    assert (await _row(x402_db, rid))[0]["status"] == "settling"


@pytest.mark.asyncio
async def test_reaper_ignores_legacy_unmarked_settling_and_notifies_once(x402_db):
    rid = await _invoice(x402_db)
    # a pre-098 claim: settling, no lane, never marked submitted
    await x402_db.execute(
        "UPDATE x402_payment_requests SET status='settling' WHERE id=?", (rid,))
    assert await invoice_reaper.reap_unsubmitted_claims(
        db=x402_db, now=time.time() + 86400) == []
    watcher = SettlementWatcher(_Agent(), db=x402_db)
    told = []

    async def push(user, text, source="", session_id=None):
        told.append(text)
    watcher._push_owner_notice = push

    async def no_held():
        return []
    watcher._resolve_held_submissions = no_held
    await watcher._sweep_stale_settling()
    await watcher._sweep_stale_settling()
    assert len(told) == 1 and rid in told[0]
    row, meta = await _row(x402_db, rid)
    assert row["status"] == "settling" and meta["held_reason"] == "settling_unknown"
    stuck = await invoice_reaper.stuck_settling(db=x402_db)
    assert [s["request_id"] for s in stuck] == [rid]
    # the owner reopens it once checked
    assert await invoice_reaper.reopen_claim(rid, db=x402_db)
    assert (await _row(x402_db, rid))[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_owner_reopen_refuses_a_submission(x402_db):
    rid = await _invoice(x402_db)
    assert await invoicing.claim_for_settlement(rid, db=x402_db)
    await x402_db.execute(
        "UPDATE x402_payment_requests SET metadata=json_set(metadata,'$.facilitator_submitted',1) "
        "WHERE id=?", (rid,))
    assert not await invoice_reaper.reopen_claim(rid, db=x402_db)


@pytest.mark.asyncio
async def test_used_unproven_escalates_once(x402_db):
    rid = await _invoice(x402_db)
    await x402_db.execute("UPDATE x402_payment_requests SET status='settling' WHERE id=?", (rid,))
    assert await invoice_reaper.escalate_once(rid, "auth_used_unproven", db=x402_db)
    assert not await invoice_reaper.escalate_once(rid, "auth_used_unproven", db=x402_db)
    _, meta = await _row(x402_db, rid)
    assert meta["held_reason"] == "auth_used_unproven" and meta["escalated_at"]


@pytest.mark.asyncio
async def test_submitted_older_than_24h_escalates(x402_db):
    rid = await _invoice(x402_db)
    assert await invoicing.claim_for_settlement(rid, db=x402_db)
    await x402_db.execute(
        "UPDATE x402_payment_requests SET metadata=json_set(metadata,'$.facilitator_submitted',1) "
        "WHERE id=?", (rid,))
    assert await invoice_reaper.escalate_stalled(db=x402_db) == []
    out = await invoice_reaper.escalate_stalled(db=x402_db, now=time.time() + 86401)
    assert [(r["request_id"], r["reason"]) for r in out] == [(rid, "submitted_stalled")]
    assert await invoice_reaper.escalate_stalled(db=x402_db, now=time.time() + 90000) == []


def test_illegal_transition_expired_to_completed_raises():
    with pytest.raises(IllegalTransition):
        X402_INVOICE.check("expired", "completed")
    assert not X402_INVOICE.validate()


async def _machine_payment(db, rid="mp1"):
    await db.execute(
        "INSERT INTO x402_payment_requests (id,amount,amount_usd,asset,chain,recipient,nonce,"
        "deadline,status,metadata) VALUES (?,'10000',0.01,'usdc','base','0xt',?,9999999999,"
        "'settling',?)",
        (rid, rid, json.dumps({"kind": "machine_payment", "tenant_id": "alice",
                               "facilitator_submitted": 1})))
    return rid


@pytest.mark.asyncio
async def test_a_machine_payment_is_never_settled_as_income(x402_db):
    """098 review: an owner settle of a held machine payment booked `completed`
    (income) although its request answered "no service was started"."""
    rid = await _machine_payment(x402_db)
    assert not await invoicing.settle_payment_request(rid, transaction_hash="0x" + "a" * 64,
                                                      db=x402_db)
    assert (await _row(x402_db, rid))[0]["status"] == "settling"


@pytest.mark.asyncio
async def test_held_notices_name_only_verbs_that_act_on_the_row(x402_db):
    rid = await _machine_payment(x402_db)
    inv_id = await _invoice(x402_db)
    assert await invoicing.claim_for_settlement(inv_id, db=x402_db)
    await x402_db.execute(
        "UPDATE x402_payment_requests SET metadata=json_set(metadata,'$.facilitator_submitted',1) "
        "WHERE id=?", (inv_id,))
    watcher = SettlementWatcher(_Agent(), db=x402_db)
    told = {}

    async def push(user, text, source="", session_id=None):
        told[[r for r in (rid, inv_id) if r in text][0]] = text
    watcher._push_owner_notice = push
    for inv in await invoice_reaper.escalate_stalled(db=x402_db, now=time.time() + 86401):
        await watcher._escalate_to_owner(inv, inv["reason"])
    assert "owner refunded" not in told[rid] and "owner settle" not in told[rid]
    assert "never counted as income" in told[rid]
    assert "owner refunded" not in told[inv_id] and "owner settle" in told[inv_id]


@pytest.mark.asyncio
async def test_owner_resolves_a_held_row(x402_db):
    """098 review O-03: a submitted settling row or an unproven settled_no_tx
    row has an owner exit — refund owed, or void after the deadline."""
    rid = await _machine_payment(x402_db, "mp9")
    assert await invoice_reaper.resolve_held(rid, paid=True, db=x402_db)
    assert (await _row(x402_db, rid))[0]["status"] == "refund_due"
    assert not await invoice_reaper.resolve_held(rid, paid=True, db=x402_db)  # once

    await x402_db.execute(
        "INSERT INTO x402_payment_requests (id,amount,amount_usd,asset,chain,recipient,nonce,"
        "deadline,status,metadata) VALUES ('nt1','1',0.01,'usdc','base','0xt','nt1',?,"
        "'settled_no_tx','{\"kind\":\"agent_invoice\"}')", (int(time.time()) + 600,))
    assert not await invoice_reaper.resolve_held("nt1", paid=False, db=x402_db)  # deadline open
    assert await invoice_reaper.resolve_held("nt1", paid=False, db=x402_db,
                                             now=time.time() + 3600)
    assert (await _row(x402_db, "nt1"))[0]["status"] == "expired"


@pytest.mark.asyncio
async def test_resolve_never_touches_an_unsubmitted_claim_or_pending(x402_db):
    rid = await _invoice(x402_db)
    assert not await invoice_reaper.resolve_held(rid, paid=True, db=x402_db)     # pending
    assert await invoicing.claim_for_settlement(rid, db=x402_db)
    assert not await invoice_reaper.resolve_held(rid, paid=True, db=x402_db)     # unsubmitted
    assert (await _row(x402_db, rid))[0]["status"] == "settling"


def test_invoice_resolve_cli_needs_an_outcome_and_confirm():
    from click.testing import CliRunner
    from cli.commands.owner import owner
    r = CliRunner().invoke(owner, ["invoice-resolve", "x", "--unpaid"])
    assert r.exit_code != 0 and "--confirm" in r.output
    r = CliRunner().invoke(owner, ["invoice-resolve", "x", "--confirm"])
    assert r.exit_code != 0 and "--unpaid or --refund-due" in r.output
