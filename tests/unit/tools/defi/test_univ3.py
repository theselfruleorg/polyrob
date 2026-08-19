"""Uniswap V3 routing provider (proposal 023 T4).

The failure this suite exists to prevent: a swap that encodes cleanly, passes
every unit test, and moves funds to the wrong place or at the wrong price. The
08-08 DeFi verification caught USDC priced at $0.44 with a green unit suite, so
these tests pin the ENCODING and the TRUST semantics, and the live gate proves
the numbers.
"""
import time

import pytest

from tools.defi.providers import univ3


def _rpc_returning(mapping):
    """Fake eth_call: fee tier (read out of the calldata) -> output amount."""
    def _rpc(method, params):
        data = params[0]["data"]
        # layout: 0x + selector(8) + tokenIn(64) + tokenOut(64) + amountIn(64) + fee(64) + limit(64)
        fee = int(data[2 + 8 + 64 * 3: 2 + 8 + 64 * 4], 16)
        out = mapping.get(fee)
        return {"result": None if out is None else "0x" + f"{out:064x}"}
    return _rpc


# --- selectors -------------------------------------------------------------

def test_selectors_match_their_signatures():
    """A wrong selector calls a different function — or none — and the value is
    simply gone. Pinned against keccak rather than trusted."""
    from eth_utils import keccak
    assert univ3._QUOTE_SELECTOR == "0x" + keccak(
        b"quoteExactInputSingle((address,address,uint256,uint24,uint160))")[:4].hex()
    assert univ3._EXACT_INPUT_SINGLE_SELECTOR == "0x" + keccak(
        b"exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))")[:4].hex()
    assert univ3._APPROVE_SELECTOR == "0x" + keccak(b"approve(address,uint256)")[:4].hex()


# --- quoting ---------------------------------------------------------------

def test_best_quote_picks_the_highest_output_tier():
    rpc = _rpc_returning({100: 1000, 500: 2500, 3000: 900, 10000: None})
    q = univ3.best_quote("base", "0x" + "11" * 20, "0x" + "22" * 20, 1_000_000, rpc=rpc)
    assert q.fee_tier == 500
    assert q.amount_out_raw == 2500


def test_best_quote_fails_open_to_none_when_nothing_routes():
    """No route must read as UNKNOWN, never as a zero-output swap — a caller
    that saw 0 could send funds for nothing."""
    rpc = _rpc_returning({100: None, 500: None, 3000: None, 10000: None})
    assert univ3.best_quote("base", "0x" + "11" * 20, "0x" + "22" * 20, 1, rpc=rpc) is None


def test_zero_output_tier_is_not_a_route():
    rpc = _rpc_returning({100: 0, 500: 0, 3000: 0, 10000: 0})
    assert univ3.best_quote("base", "0x" + "11" * 20, "0x" + "22" * 20, 1, rpc=rpc) is None


def test_quote_carries_a_timestamp_for_the_freshness_window():
    rpc = _rpc_returning({500: 42})
    before = time.time()
    q = univ3.best_quote("base", "0x" + "11" * 20, "0x" + "22" * 20, 1, rpc=rpc)
    assert before <= q.quoted_at <= time.time()


def test_quote_survives_an_rpc_exception():
    def _boom(method, params):
        raise RuntimeError("rpc down")
    assert univ3.quote_single("base", "0x" + "11" * 20, "0x" + "22" * 20, 1, 500,
                              rpc=_boom) is None


# --- calldata encoding -----------------------------------------------------

def test_exact_input_single_encodes_seven_words_and_no_deadline():
    """SwapRouter02's struct has NO deadline (SwapRouter01 did). An extra word
    shifts every field after it and the call reverts."""
    data = univ3.build_exact_input_single_data(
        token_in="0x" + "11" * 20, token_out="0x" + "22" * 20, fee=500,
        recipient="0x" + "33" * 20, amount_in_raw=1_000_000, amount_out_min_raw=999)
    assert data.startswith(univ3._EXACT_INPUT_SINGLE_SELECTOR)
    assert len(data) == 2 + 8 + 7 * 64
    words = [data[2 + 8 + i * 64: 2 + 8 + (i + 1) * 64] for i in range(7)]
    assert words[0].endswith("11" * 20)          # tokenIn
    assert words[1].endswith("22" * 20)          # tokenOut
    assert int(words[2], 16) == 500              # fee
    assert words[3].endswith("33" * 20)          # recipient
    assert int(words[4], 16) == 1_000_000        # amountIn
    assert int(words[5], 16) == 999              # amountOutMinimum
    assert int(words[6], 16) == 0                # sqrtPriceLimitX96


def test_approve_encodes_the_exact_amount_it_was_given():
    data = univ3.build_approve_data(spender="0x" + "44" * 20, amount_raw=12345)
    assert data.startswith(univ3._APPROVE_SELECTOR)
    assert len(data) == 2 + 8 + 2 * 64
    assert int(data[2 + 8 + 64:], 16) == 12345


def test_approve_never_encodes_unlimited_by_itself():
    """The module has no 'infinite' path — an unlimited approval can only come
    from a caller explicitly passing MAX_UINT256, which the tool layer refuses."""
    import inspect
    src = inspect.getsource(univ3)
    assert "2**256" not in src and "ffffffffffffffff" not in src.lower()


# --- addresses -------------------------------------------------------------

def test_router_and_quoter_are_distinct_checksummed_base_addresses():
    """Pinned so a copy-paste edit cannot quietly point execution somewhere
    else; both are verified to carry code on Base."""
    assert univ3.QUOTER_V2 == "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
    assert univ3.SWAP_ROUTER_02 == "0x2626664c2603336E57B271c5C0b26F421741e481"
    assert univ3.QUOTER_V2 != univ3.SWAP_ROUTER_02


# --- multi-chain addressing (2026-08-17) ------------------------------------
# Ethereum's Uniswap V3 deployment lives at DIFFERENT addresses from Base's.
# Quoting Base's quoter address on Ethereum reads a contract that is not there;
# routing a swap to Base's router on Ethereum sends funds to codeless space and
# does NOT error. Every address must come from the chain's own registry row.

def test_the_quoter_address_is_the_chains_own():
    seen = {}

    def _rpc(method, params):
        seen[params[0]["to"]] = True
        return {"result": None}

    univ3.quote_single("ethereum", "0x" + "11" * 20, "0x" + "22" * 20,
                       1_000_000, 500, rpc=_rpc)
    assert "0x61fFE014bA17989E743c5F6cB21bF9697530B21e" in seen
    assert univ3.QUOTER_V2 not in seen, "base's quoter must never be used on ethereum"


def test_the_quote_carries_the_chains_own_router():
    rpc = _rpc_returning({500: 2500})
    q = univ3.best_quote("ethereum", "0x" + "11" * 20, "0x" + "22" * 20,
                         1_000_000, rpc=rpc)
    assert q.router == "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"
    assert q.router != univ3.SWAP_ROUTER_02


def test_a_chain_with_no_verified_deployment_does_not_quote():
    """Robinhood has no Uniswap V3 deployment. Quoting must return None (no
    route) rather than calling Base's address on a chain where it is empty."""
    def _boom(method, params):
        raise AssertionError("must not reach the network for a chain with no DEX")

    assert univ3.quote_single("robinhood", "0x" + "11" * 20, "0x" + "22" * 20,
                              1_000_000, 500, rpc=_boom) is None
    assert univ3.best_quote("robinhood", "0x" + "11" * 20, "0x" + "22" * 20,
                            1_000_000, rpc=_boom) is None


def test_allowance_read_fails_open_to_none():
    def _boom(method, params):
        raise RuntimeError("rpc down")
    assert univ3.read_allowance("base", "0x" + "11" * 20, "0x" + "22" * 20,
                                "0x" + "33" * 20, rpc=_boom) is None


# --- RPC trust anchor -------------------------------------------------------

def test_rpc_prefers_the_operator_pinned_endpoint(monkeypatch):
    """The quote anchors amountOutMinimum — the swap's only on-chain price
    protection — so it must come from the SAME pinned endpoint the guard
    simulates against, never the shared public default when a real one is
    configured."""
    seen = {}

    class _Resp:
        def read(self):
            return b'{"result": null}'

    def fake_urlopen(req, timeout=15.0):
        seen["url"] = req.full_url
        return _Resp()

    monkeypatch.setenv("DEFI_EVM_RPC_BASE", "https://pinned.example/rpc")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    univ3._rpc("base", "eth_call", [])
    assert seen["url"] == "https://pinned.example/rpc"
