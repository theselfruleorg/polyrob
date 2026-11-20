"""F8: a seller that answers 402 to a SIGNED authorization rejected the payment.

The authorization stays valid until it expires, so it may still be submitted —
the payer books its signed ceiling (the one booking path, `policy.record`) and
the journal row is booked: the payer's other rails are free at once instead of
refused until the authorization expires. The same request stays a replay.
"""
import json
import os

import pytest

from core.wallet import submission_journal as sj
from tools.x402.client import X402Result
from tools.x402.service import FetchParams, X402PayTool
from tests.unit.tools.x402.test_085_w4_book_what_we_signed import _ResultClient, _wallet

URL = "https://seller.example/paid"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    return str(tmp_path)


def _rejected(ref, *, sent=True):
    return X402Result(body="payment rejected", paid=False, amount_usd=0.0, tx_hash=None,
                      pay_to="0x" + "b" * 40, status_code=402, submission_ref=ref,
                      authorized_amount_usd=0.05, payment_sent=sent)


@pytest.mark.asyncio
async def test_a_rejected_signed_payment_is_booked_and_frees_the_rails(home):
    from core.money.ledger import SpendLedger
    from core.wallet.audit_sink import default_audit_sink
    wallet = _wallet()
    wallet._policy = SpendLedger(max_per_tx_usd=10, daily_cap_usd=100,
                                 audit_sink=default_audit_sink(home))
    seen = {}

    class _Client(_ResultClient):
        async def fetch_with_payment(self, **kwargs):
            # the real client journals the attempt before the SDK signs it
            ref = sj.prepare_attempt("x402", "0x" + "1" * 40, 0.05,
                                     idempotency_key=kwargs["idempotency_key"])
            seen["blocked"] = [r["tx_hash"] for r in sj.unresolved(home)] == [ref]
            seen["ref"] = ref
            return _rejected(ref)

    out = await X402PayTool(wallet=wallet, client=_Client(None)).x402_fetch(
        FetchParams(url=URL, max_amount_usd=1.0))
    ref = seen["ref"]
    assert seen["blocked"]                                       # every rail was refused

    assert out.error and "PAYMENT REJECTED" in out.error and "$0.0500" in out.error
    assert sj.unresolved(home) == []                              # the rails are free
    with open(os.path.join(home, "wallet", "audit.jsonl"), encoding="utf-8") as fh:
        (entry,) = [json.loads(line) for line in fh if line.strip()]
    assert entry["action"] == "pay_rejected" and entry["amount_usd"] == pytest.approx(0.05)
    assert entry["submission_ref"] == ref
    # the same request is a replay (the old authorization may still land) …
    assert not wallet.policy.check(venue="x402", amount_usd=0.05,
                                   idempotency_key=entry["idempotency_key"]).allowed
    # … any other payment passes
    assert wallet.policy.check(venue="x402", amount_usd=0.05, idempotency_key="other").allowed
    sj.prepare_attempt("x402", "0x" + "1" * 40, 0.05)


@pytest.mark.asyncio
async def test_an_unsent_attempt_keeps_the_unconfirmed_text():
    """No payload left the process: not a rejection; the old fail-closed text stays."""
    wallet = _wallet()
    out = await X402PayTool(wallet=wallet, client=_ResultClient(
        _rejected("attempt:x", sent=False))).x402_fetch(FetchParams(url=URL, max_amount_usd=1.0))
    assert out.error and "unconfirmed" in out.error and "REJECTED" not in out.error
    assert not wallet.policy.audit_log


@pytest.mark.asyncio
async def test_a_booking_failure_keeps_the_row(monkeypatch):
    wallet = _wallet()

    def boom(**kw):
        raise RuntimeError("ledger down")
    monkeypatch.setattr(wallet.policy, "record", boom)
    out = await X402PayTool(wallet=wallet, client=_ResultClient(
        _rejected("attempt:y"))).x402_fetch(FetchParams(url=URL, max_amount_usd=1.0))
    assert out.error and "unconfirmed" in out.error
