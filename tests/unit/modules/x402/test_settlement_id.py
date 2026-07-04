"""F2: deterministic settlement payment-id so the middleware can ALWAYS record
a payment (incl. the tx-less case) and a retry of the same settlement dedups."""
from modules.x402.x402_integration import settlement_payment_id


def test_uses_tx_hash_when_present():
    pid = settlement_payment_id("0xDEADBEEFcafebabe1234", "0xpayer", "/a2a/rpc", 100)
    assert pid == "x402_0xDEADBEEFcafeba"  # x402_ + first 16 chars of tx


def test_txless_is_deterministic_for_same_window():
    a = settlement_payment_id(None, "0xpayer", "/a2a/rpc", 100)
    b = settlement_payment_id(None, "0xpayer", "/a2a/rpc", 100)
    assert a == b
    assert a.startswith("x402_notx_")


def test_txless_differs_by_payer_path_and_window():
    base = settlement_payment_id(None, "0xpayer", "/a2a/rpc", 100)
    assert settlement_payment_id(None, "0xother", "/a2a/rpc", 100) != base
    assert settlement_payment_id(None, "0xpayer", "/task/run", 100) != base
    assert settlement_payment_id(None, "0xpayer", "/a2a/rpc", 101) != base
