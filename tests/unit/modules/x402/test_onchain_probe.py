"""On-chain USDC settlement detection probe (Task 11, Phase 2) — pure logic,
no real chain: `rpc` is always an injected fake callable."""
from modules.x402 import onchain_probe

TREASURY = "0x000000000000000000000000000000000000ab"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def _pad(addr: str) -> str:
    return "0x" + addr.lower().replace("0x", "").rjust(64, "0")


def _log(tx_hash, from_addr, value_atomic, block, to_addr=TREASURY):
    return {
        "address": USDC,
        "topics": [onchain_probe.TRANSFER_TOPIC, _pad(from_addr), _pad(to_addr)],
        "data": hex(value_atomic),
        "blockNumber": hex(block),
        "transactionHash": tx_hash,
    }


def test_scan_parses_transfer_logs_with_correct_amount():
    from_addr = "0x" + "1a" * 20  # a valid 40-hex-char (20-byte) address
    logs = [_log("0xabc", from_addr, 12_340000, 100)]

    def fake_rpc(method, params):
        assert method == "eth_getLogs"
        f = params[0]
        assert f["address"] == USDC
        assert f["fromBlock"] == hex(90)
        assert f["toBlock"] == hex(110)
        assert f["topics"][0] == onchain_probe.TRANSFER_TOPIC
        assert f["topics"][1] is None
        assert f["topics"][2] == _pad(TREASURY)
        return logs

    out = onchain_probe.scan_treasury_transfers(fake_rpc, USDC, TREASURY, 90, 110)
    # 046 Phase 0 added `amount_raw` — the EXACT integer settlement now matches
    # on. `amount_usd` is unchanged and is display-only.
    assert out == [{
        "tx_hash": "0xabc", "from": from_addr, "amount_raw": 12_340000,
        "amount_usd": 12.34, "block": 100,
    }]


def test_scan_rpc_error_returns_none_no_raise():
    """An RPC failure is UNKNOWN (None), not an empty range — and still never
    raises into the watcher tick.

    Contract changed 2026-08-07 (audit #1): this previously returned [], which
    the watcher could not distinguish from "scanned, nothing there", so it
    advanced the checkpoint past an unread range. See
    tests/unit/modules/x402/test_scan_checkpoint_honesty.py.
    """
    def bad_rpc(method, params):
        raise RuntimeError("rpc down")

    assert onchain_probe.scan_treasury_transfers(bad_rpc, USDC, TREASURY, 1, 10) is None


def test_scan_no_logs_returns_empty_list():
    assert onchain_probe.scan_treasury_transfers(
        lambda m, p: [], USDC, TREASURY, 1, 10) == []


def test_scan_from_after_to_short_circuits_without_calling_rpc():
    calls = []

    def rpc(method, params):
        calls.append(method)
        return []

    assert onchain_probe.scan_treasury_transfers(rpc, USDC, TREASURY, 100, 10) == []
    assert calls == []


def test_scan_malformed_log_entry_is_skipped_not_raised():
    bad = {"topics": [onchain_probe.TRANSFER_TOPIC], "data": "0x1",
           "blockNumber": "0x1", "transactionHash": "0xbad"}
    good = _log("0xgood", "0x2222222222222222222222222222222222222b", 5_000000, 50)

    out = onchain_probe.scan_treasury_transfers(
        lambda m, p: [bad, good], USDC, TREASURY, 1, 100)
    assert len(out) == 1
    assert out[0]["tx_hash"] == "0xgood"
    assert out[0]["amount_usd"] == 5.0


def test_get_head_block_parses_hex_result():
    assert onchain_probe.get_head_block(lambda m, p: "0x64") == 100


def test_get_head_block_rpc_error_returns_none():
    def rpc(method, params):
        raise RuntimeError("down")

    assert onchain_probe.get_head_block(rpc) is None


def test_get_head_block_empty_result_returns_none():
    assert onchain_probe.get_head_block(lambda m, p: None) is None


# --- 046 Phase 0: exact raw integers at the asset's decimals -----------------

def _tlog(value_hex, frm="0x" + "22" * 20, block=5, tx="0xdead"):
    from modules.x402.onchain_probe import TRANSFER_TOPIC, _pad_address_topic
    return {
        "topics": [TRANSFER_TOPIC, _pad_address_topic(frm),
                   _pad_address_topic("0x" + "11" * 20)],
        "data": value_hex,
        "transactionHash": tx,
        "blockNumber": hex(block),
    }


def test_an_18_decimal_transfer_returns_an_exact_raw_integer():
    """⚠️ A value no float can hold exactly. Matching on amount_usd was safe
    only while every asset had 6 decimals."""
    from modules.x402 import onchain_probe
    value = 7 * 10 ** 18 + 3
    rows = onchain_probe.scan_treasury_transfers(
        lambda m, p: [_tlog(hex(value))], "0x" + "cc" * 20, "0x" + "11" * 20,
        1, 9, decimals=18)
    assert rows[0]["amount_raw"] == value


def test_the_default_decimals_keep_the_usdc_shape_byte_identical():
    from modules.x402 import onchain_probe
    rows = onchain_probe.scan_treasury_transfers(
        lambda m, p: [_tlog(hex(1_250_000))], "0x" + "cc" * 20,
        "0x" + "11" * 20, 1, 9)
    assert rows[0]["amount_usd"] == 1.25
    assert rows[0]["amount_raw"] == 1_250_000


def test_an_rpc_failure_still_returns_none_not_an_empty_list():
    """⚠️ The return contract is load-bearing: None = NOT scanned (hold the
    checkpoint), [] = scanned and empty."""
    from modules.x402 import onchain_probe

    def boom(m, p):
        raise RuntimeError("rpc down")

    assert onchain_probe.scan_treasury_transfers(
        boom, "0x" + "cc" * 20, "0x" + "11" * 20, 1, 9, decimals=18) is None


def test_a_scanned_empty_range_is_an_empty_list_not_none():
    from modules.x402 import onchain_probe
    assert onchain_probe.scan_treasury_transfers(
        lambda m, p: [], "0x" + "cc" * 20, "0x" + "11" * 20, 1, 9,
        decimals=18) == []
