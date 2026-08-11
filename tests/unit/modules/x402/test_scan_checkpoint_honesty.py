"""A failed on-chain scan must not advance the settlement checkpoint.

Audit 2026-08-07, finding #1 (Critical): `scan_treasury_transfers` returned []
on RPC failure, indistinguishable from "no transfers in range", and
`_scan_onchain` advanced the checkpoint unconditionally. A rate-limited
`eth_getLogs` therefore burned a block range containing a real payment —
permanently, because `advance_scan_checkpoint` refuses to regress. The payer's
money arrives, the invoice expires unpaid, and no `payment_unmatched` fires
because the transfer was never enumerated.

Contract: scan failure -> None (unknown). Genuinely empty range -> [].
"""
import modules.x402.onchain_probe as probe

TREASURY = "0x" + "33" * 20
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def test_scan_returns_none_when_the_rpc_fails():
    def boom(method, params):
        raise RuntimeError("block range too large")

    out = probe.scan_treasury_transfers(boom, USDC, TREASURY, 100, 200)
    assert out is None, "a failed scan must be UNKNOWN, not an empty result"


def test_scan_returns_empty_list_for_a_genuinely_empty_range():
    def empty(method, params):
        return []

    out = probe.scan_treasury_transfers(empty, USDC, TREASURY, 100, 200)
    assert out == [], "no transfers is a real answer and stays []"


def test_inverted_range_is_empty_not_unknown():
    def unused(method, params):
        raise AssertionError("must not call the RPC for an inverted range")

    assert probe.scan_treasury_transfers(unused, USDC, TREASURY, 200, 100) == []
