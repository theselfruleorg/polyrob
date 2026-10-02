"""Post-ship regression corpus and real invoice settlement proof."""
import pytest
from core.rails.financial_claims import RecordsRead, detect_claim, verify_claim
NEGATIVE = [
'I paid attention to the error message.',
'We paid close attention to the invoice warning.',
'I paid tribute to the volunteers.',
'The payment failed with a timeout.',
'I tried to pay $5, but the request failed.',
'Payment pending: awaiting confirmation for $5.',
'I will pay $5 after approval.',
'Could you confirm whether the $5 invoice was paid?',
'No payment was made.',
'We earned $0 today.',
'I sent you the invoice for $5.',
'Invoice created for $5; awaiting payment.',
'The quoted price is $5 per request.',
'The vendor says "payment completed" in its example response.',
'The documentation example reads "I paid $5".',
'The owner wrote: "I paid $5 yesterday".',
'The error is: "payment completed" was reported without a receipt.',
'I have not paid the $5 invoice.',
'The transaction is unconfirmed.',
'I attempted the transfer of 0.1 ETH.',
'The transfer failed; funds remain in the wallet.',
'We settled on the blue logo.',
'I received your email.',
'I bought time by asking for an extension.',
'I spent three hours reviewing the payment code.',
'The payment simulation completed on testnet.',
'Target: first revenue of $10.',
'The $5 payment was refunded.',
'Invoice unpaid: $10 remains due.',
'I cannot read the ledger; the audit store is unavailable.',
]
POSITIVE = [
'I paid $5 to the endpoint.',
'We earned $12.50 today.',
'Payment completed: $2 to the vendor.',
'I just sent 0.01 ETH to the treasury.',
'Received a payment of 3 USDC.',
'The transaction completed for $5.',
'We successfully paid $8 for the subscription.',
'I transferred 10 USDC to the customer.',
'We spent $5 on compute.',
'First x402 micro-transaction completed!',
'Invoice was settled for $5.',
'We collected $10 from sales.',
'I generated revenue of $15.',
'We got paid $7.',
'Payment went through for $4.',
'I purchased $3 of API credits.',
'The swap succeeded: 0.1 ETH.',
'We received funds of $20.',
'I completed the payment of $6.',
'We made our first sale for $10.',
]


@pytest.mark.parametrize('text', NEGATIVE)
def test_non_payment_reports_remain_sendable(text):
    assert verify_claim(detect_claim(text), RecordsRead(unreadable=['audit unavailable'])) is None

@pytest.mark.parametrize('text', POSITIVE)
def test_unbacked_money_movement_is_refused(text):
    assert verify_claim(detect_claim(text), RecordsRead()) is not None

@pytest.mark.parametrize('prefix', NEGATIVE)
def test_report_does_not_hide_a_separate_money_claim(prefix):
    assert detect_claim(prefix + ' I paid $5 to the endpoint.') is not None

@pytest.mark.asyncio
async def test_real_completed_invoice_satisfies_incoming_claim(tmp_path, monkeypatch):
    from tests.unit.modules.x402.test_invoicing import _setup_db
    from modules.x402 import invoicing
    from tools.controller import financial_claim_gate as gate
    monkeypatch.setenv('X402_PAYMENT_RECIPIENT', '0xTREASURY')
    monkeypatch.setenv('X402_DEFAULT_CHAIN', 'base')
    monkeypatch.setenv('X402_INVOICE_AMOUNT_JITTER', 'false')
    db=await _setup_db(tmp_path)
    async def resolve(): return db
    monkeypatch.setattr('modules.x402._db.resolve_db', resolve)
    try:
        inv=await invoicing.create_payment_request(user_id='u1',session_id='s1',amount_usd=5,purpose='test',db=db)
        await invoicing.settle_payment_request(inv['request_id'],transaction_hash='0xabc',db=db)
        records,error=await gate._receive_records('u1')
        assert error is None
        assert verify_claim(detect_claim('We received a payment of $5.'),RecordsRead(records=records)) is None
        other,error=await gate._receive_records('other')
        assert other == []
    finally: await db.close()

@pytest.mark.asyncio
async def test_unreadable_invoice_service_is_not_reported_empty(monkeypatch):
    from tools.controller import financial_claim_gate as gate
    async def broken(): raise OSError('unreadable')
    monkeypatch.setattr('modules.x402._db.resolve_db',broken)
    records,error=await gate._receive_records('u1')
    assert records == []
    assert error == 'the settled invoices'
