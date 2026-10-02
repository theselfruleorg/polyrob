"""CR-H06 (Permit2 decode), CR-L05, CR-L16, CR-L17 — guard + simulation.

* H06: a Permit2 ``Approval``/``Permit`` (topic0 differs from ERC-20 Approval,
  four topics) was never decoded, so an undeclared Permit2 grant passed.
* L05: a token-only claim had no native-outflow assertion.
* L16: ``Approval(owner, 0x0, id)`` on transfer (OZ < 4.8, Uniswap NPM) was
  refused as an undeclared grant.
* L17: an oversized ERC-1155 TransferBatch or a 1-topic legacy Transfer
  touching the holder was silently skipped.
"""
from core.wallet import simulation, tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas

HOLDER = "0x2222222222222222222222222222222222222222"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
NFT = "0x4444444444444444444444444444444444444444"
BUYER = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
SPENDER = "0x9999999999999999999999999999999999999999"
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
ESCROW = "0xd3afeb2a57f70ef218aa82451c51b2fb0416ac9e"
ETH = 10 ** 18

T_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
T_BATCH = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"
T_P2_APPROVAL = "0xda9fa7c1b00402c17d0161b249b1ab8bbec047c5a52207b9c112deffd817036b"


def _w(n):
    return f"{n:064x}"


def _ta(addr):
    return "0x" + addr[2:].lower().rjust(64, "0")


def _sim(*logs):
    ok = lambda v: {"status": "0x1", "returnData": "0x" + _w(v)}  # noqa: E731
    entry = {"status": "0x1", "returnData": "0x", "logs": list(logs)}
    entries = [{"calls": [ok(ETH), ok(1000), entry, ok(ETH), ok(1000)]}]
    return simulation.simulate(
        {"to": NFT, "data": "0x", "value": 0, "chainId": 8453},
        holder=HOLDER, chain="base", tokens=[USDC], spenders=[],
        rpc=lambda m, p, timeout=8.0: entries)


def _guard(intent, deltas, *, to=NFT, price=1.0):
    return tx_guard.authorize(
        intent, {"to": to, "data": "0x4e71d92d", "value": 0, "chainId": 8453,
                 "nonce": 1, "gas": 100_000, "maxFeePerGas": 10 ** 9},
        holder=HOLDER, gate=PolicyGate(max_per_tx_usd=500.0, daily_cap_usd=1000.0),
        execution_context=None, simulate_fn=lambda **_: deltas,
        price_fn=lambda c, a: price, rpc_is_pinned_fn=lambda c: True,
        halted_fn=lambda: False, entry_paused_fn=lambda: False,
        forged_fn=lambda c, t: False,
        # every destination is an EOA (W2 reads its code)
        account_rpc=lambda m, p: "0x")


# --- H06: Permit2 ----------------------------------------------------------

def test_simulation_decodes_a_permit2_approval_of_the_holder():
    log = {"address": PERMIT2, "topics": [T_P2_APPROVAL, _ta(HOLDER), _ta(USDC),
                                          _ta(SPENDER)],
           "data": "0x" + _w(10 ** 12) + _w(2 ** 40)}
    d = _sim(log)
    assert d.ok, d.error
    assert d.holder_permit2_grants == (
        (PERMIT2.lower(), USDC.lower(), SPENDER.lower(), 10 ** 12),)


def test_guard_refuses_an_undeclared_permit2_grant():
    intent = tx_guard.TxIntent(chain="base", token=USDC, to=SPENDER,
                               amount_raw=1_000_000, max_spend_usd=5.0,
                               idempotency_key="k")
    deltas = Deltas(ok=True, native_delta=0, token_deltas={USDC: -1_000_000},
                    holder_permit2_grants=((PERMIT2.lower(), USDC.lower(),
                                            SPENDER.lower(), 10 ** 12),))
    d = _guard(intent, deltas, to=SPENDER)
    assert not d.allowed
    assert "Permit2" in d.reason


# --- L05: claim native outflow ---------------------------------------------

def test_a_token_claim_that_drains_native_is_refused():
    intent = tx_guard.TxIntent(chain="base", token=None, to=ESCROW, amount_raw=0,
                               max_spend_usd=5.0, idempotency_key="c",
                               is_claim=True, inflow_token=USDC,
                               min_inflow_raw=1_000_000)
    deltas = Deltas(ok=True, native_delta=-ETH, token_deltas={USDC: 1_000_000},
                    gas_used=40_000)
    d = _guard(intent, deltas, to=ESCROW)
    assert not d.allowed
    assert "native" in d.reason


# --- L16: approval cleared to zero on transfer -----------------------------

def test_an_nft_transfer_that_clears_the_approval_to_zero_is_allowed():
    intent = tx_guard.TxIntent(chain="base", token=None, to=NFT, amount_raw=0,
                               max_spend_usd=5.0, idempotency_key="n",
                               is_nft_op=True, nft_out=((NFT, "erc721", 42, 1),))
    zero = "0x0000000000000000000000000000000000000000"
    deltas = Deltas(ok=True, native_delta=0, gas_used=90_000,
                    holder_nft_out=((NFT.lower(), "erc721", BUYER.lower(), 42, 1),),
                    holder_nft_approvals=((NFT.lower(), zero, 42),))
    d = _guard(intent, deltas)
    assert d.allowed, d.reason


# --- L17: unreadable NFT events touching the holder -------------------------

def test_an_oversized_transfer_batch_from_the_holder_refuses_the_simulation():
    n = simulation._MAX_BATCH_IDS + 1
    data = ("0x" + _w(64) + _w(64 + 32 * (n + 1)) + _w(n) + "".join(_w(i) for i in range(n))
            + _w(n) + "".join(_w(1) for _ in range(n)))
    log = {"address": NFT, "topics": [T_BATCH, _ta(BUYER), _ta(HOLDER), _ta(BUYER)],
           "data": data}
    d = _sim(log)
    assert not d.ok
    assert "cannot be read" in d.error


def test_a_one_topic_legacy_transfer_naming_the_holder_refuses_the_simulation():
    log = {"address": NFT, "topics": [T_TRANSFER],
           "data": "0x" + _w(int(HOLDER, 16)) + _w(int(BUYER, 16)) + _w(7)}
    d = _sim(log)
    assert not d.ok


def test_a_legacy_transfer_not_touching_the_holder_is_still_ignored():
    log = {"address": NFT, "topics": [T_TRANSFER],
           "data": "0x" + _w(int(SPENDER, 16)) + _w(int(BUYER, 16)) + _w(7)}
    assert _sim(log).ok
