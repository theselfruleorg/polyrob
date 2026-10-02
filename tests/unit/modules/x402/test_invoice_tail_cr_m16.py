"""CR-M16: every EVM invoice gets a random sub-cent tail at creation, so a
round-amount transfer (an owner top-up, a venue withdrawal) does not settle
a stranger's invoice."""
import pytest

from modules.x402 import invoice_jitter, invoicing

TREASURY = "0xTreasuryAddress000000000000000000000001"


@pytest.fixture(autouse=True)
def _tail_on(monkeypatch):
    monkeypatch.setattr(invoice_jitter, "TAIL_ENABLED", True)
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", TREASURY)
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    for var in ("X402_INVOICE_MAX_USD", "X402_INVOICE_DAILY_MAX"):
        monkeypatch.delenv(var, raising=False)


def test_usd_tail_is_sub_cent_within_six_decimals():
    for _ in range(200):
        v = invoice_jitter.random_tail_usd(5.0, 100.0, 6)
        assert 5.0 < v < 5.01
        assert round(v, 6) == v


def test_usd_tail_goes_down_at_the_cap():
    for _ in range(50):
        v = invoice_jitter.random_tail_usd(100.0, 100.0, 6)
        assert 99.99 < v < 100.0


def test_usd_tail_is_skipped_when_the_asset_cannot_express_it():
    assert invoice_jitter.random_tail_usd(5.0, 100.0, 2) == 5.0


def test_raw_tail_is_worth_less_than_a_cent_and_respects_the_step():
    # USDC: $0.50 = 500000 raw; tail < 10000 raw.
    for _ in range(100):
        t = invoice_jitter.random_tail_raw(500_000, 0.5, 6)
        assert 1 <= t < 10_000
    # 18-decimal token at $0.0001: a cent is 100 tokens; tail in whole tokens.
    raw = 5000 * 10 ** 18
    for _ in range(100):
        t = invoice_jitter.random_tail_raw(raw, 0.5, 18)
        assert t % 10 ** 18 == 0 and 1 <= t // 10 ** 18 < 100
    # A step already worth a cent or more: no tail.
    assert invoice_jitter.random_tail_raw(5 * 10 ** 18, 5.0, 18) == 0


@pytest.mark.asyncio
async def test_created_evm_invoice_is_not_a_round_amount(x402_db):
    inv = await invoicing.create_payment_request(
        user_id="rob", session_id="s", amount_usd=5.0, purpose="a", db=x402_db)
    row = await x402_db.fetch_one(
        "SELECT amount_usd, amount_raw FROM x402_payment_requests WHERE id=?",
        (inv["request_id"],))
    assert 5.0 < float(row["amount_usd"]) < 5.01
    assert int(row["amount_raw"]) != 5_000_000
    assert int(row["amount_raw"]) == round(float(row["amount_usd"]) * 10 ** 6)
