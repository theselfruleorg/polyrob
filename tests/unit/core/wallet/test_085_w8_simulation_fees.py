"""085 W8 txguard LEAD-1 — the EVM simulation reads like an execution.

``validation: true``, a real gas limit and ``maxFeePerGas``, and
``blockOverrides.baseFeePerGas`` = the latest block's, so ``block.basefee`` and
``tx.gasprice`` are not 0 in the simulation. The payer's balance gets exactly the
gas headroom (a state override), and the gas it is charged is added back, so the
native deltas still measure value only.
"""
from core.wallet import simulation
from core.wallet.onchain import MULTICALL3

HOLDER = "0x2222222222222222222222222222222222222222"
SENDER = "0x3333333333333333333333333333333333333333"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ETH = 10 ** 18
BASE_FEE = 50 * 10 ** 9
GAS_USED = 120_000


def _word(n):
    return "0x" + f"{n:064x}"


def _ok(v):
    return {"status": "0x1", "returnData": _word(v)}


class _Node:
    """A fake node: the latest block, balances, and one bundle answer."""

    def __init__(self, entries, *, base_fee=BASE_FEE, balance=ETH, block=True):
        self.entries = entries
        self.base_fee = base_fee
        self.balance = balance
        self.block = block
        self.calls = []

    def __call__(self, method, params, timeout=8.0):
        self.calls.append((method, params))
        if method == "eth_getBlockByNumber":
            if not self.block:
                raise RuntimeError("rpc down")
            return {"baseFeePerGas": hex(self.base_fee), "gasLimit": hex(30_000_000)}
        if method == "eth_getBalance":
            return hex(self.balance)
        if method == "eth_simulateV1":
            return self.entries
        raise AssertionError(method)

    def bundle(self):
        return next(p for m, p in self.calls if m == "eth_simulateV1")[0]


def _tx(value=0):
    return {"to": USDC, "data": "0xa9059cbb", "value": value, "chainId": 8453}


def _tx_entry(gas_used=GAS_USED):
    entry = {"status": "0x1", "returnData": "0x", "logs": []}
    if gas_used is not None:
        entry["gasUsed"] = hex(gas_used)
    return entry


def test_the_bundle_runs_validated_at_the_latest_base_fee():
    node = _Node([{"calls": [_ok(ETH), _ok(1000), _tx_entry(),
                             _ok(ETH - GAS_USED * BASE_FEE), _ok(750)]}])
    d = simulation.simulate(_tx(), holder=HOLDER, chain="base", tokens=[USDC],
                            spenders=[], rpc=node)
    assert d.ok is True
    sim = node.bundle()
    assert sim["validation"] is True
    block = sim["blockStateCalls"][0]
    assert block["blockOverrides"]["baseFeePerGas"] == hex(BASE_FEE)
    calls = block["calls"]
    for call in calls:
        assert int(call["gas"], 16) > 0
        assert int(call["maxFeePerGas"], 16) >= BASE_FEE
    the_tx = calls[2]
    assert the_tx["from"] == HOLDER
    # The reads pay their own gas from the synthetic reader, never from the holder.
    assert calls[0]["to"] == MULTICALL3
    assert calls[0]["from"] == simulation._SIM_READER
    # The payer gets exactly the gas headroom on top of its real balance.
    overrides = block["stateOverrides"]
    headroom = int(the_tx["maxFeePerGas"], 16) * int(the_tx["gas"], 16)
    assert int(overrides[HOLDER]["balance"], 16) == ETH + headroom
    assert int(overrides[simulation._SIM_READER]["balance"], 16) > 0


def test_the_gas_charge_is_not_a_native_outflow():
    """A token transfer: the holder pays only gas, and the native delta reads 0."""
    node = _Node([{"calls": [_ok(ETH), _ok(1000), _tx_entry(),
                             _ok(ETH - GAS_USED * BASE_FEE), _ok(750)]}])
    d = simulation.simulate(_tx(), holder=HOLDER, chain="base", tokens=[USDC],
                            spenders=[], rpc=node)
    assert d.native_delta == 0
    assert d.token_deltas[USDC] == -250
    assert d.gas_used == GAS_USED


def test_a_native_send_still_measures_its_value():
    value = ETH // 10
    node = _Node([{"calls": [_ok(ETH), _ok(1000), _tx_entry(),
                             _ok(ETH - value - GAS_USED * BASE_FEE), _ok(1000)]}])
    d = simulation.simulate(_tx(value), holder=HOLDER, chain="base", tokens=[USDC],
                            spenders=[], rpc=node)
    assert d.native_delta == -value


def test_a_hidden_native_drain_is_still_seen():
    node = _Node([{"calls": [_ok(ETH), _ok(1000), _tx_entry(),
                             _ok(ETH // 2 - GAS_USED * BASE_FEE), _ok(1000)]}])
    d = simulation.simulate(_tx(), holder=HOLDER, chain="base", tokens=[USDC],
                            spenders=[], rpc=node)
    assert d.native_delta == -(ETH - ETH // 2)


def test_the_signer_pays_the_gas_when_it_is_not_the_holder():
    """050: the owner's key signs for the account; its gas is added back, the
    account's native read is untouched."""
    node = _Node([{"calls": [_ok(ETH), _ok(1000), _ok(5 * ETH), _tx_entry(),
                             _ok(ETH), _ok(1000), _ok(5 * ETH - GAS_USED * BASE_FEE)]}],
                 balance=5 * ETH)
    d = simulation.simulate(_tx(), holder=HOLDER, chain="base", tokens=[USDC],
                            spenders=[], rpc=node, sender=SENDER)
    assert d.ok is True
    assert d.sender_native_delta == 0
    assert d.native_delta == 0
    block = node.bundle()["blockStateCalls"][0]
    assert block["calls"][3]["from"] == SENDER
    assert SENDER in block["stateOverrides"]
    assert ("eth_getBalance", [SENDER, "latest"]) in node.calls


def test_an_unreadable_base_fee_refuses():
    node = _Node([], block=False)
    d = simulation.simulate(_tx(), holder=HOLDER, chain="base", tokens=[USDC],
                            spenders=[], rpc=node)
    assert d.ok is False
    assert "base fee" in d.error
    assert all(m != "eth_simulateV1" for m, _ in node.calls)


def test_a_missing_gas_used_refuses_when_gas_was_charged():
    node = _Node([{"calls": [_ok(ETH), _ok(1000), _tx_entry(gas_used=None),
                             _ok(ETH - GAS_USED * BASE_FEE), _ok(750)]}])
    d = simulation.simulate(_tx(), holder=HOLDER, chain="base", tokens=[USDC],
                            spenders=[], rpc=node)
    assert d.ok is False
    assert "gasUsed" in d.error


def test_a_placeholder_gas_limit_never_starves_the_simulation():
    """Live 2026-10-19..20: the built swap carried the broadcaster's fixed 120k
    gas, the route needs ~341k, and the validated simulation ran out of gas
    ("execution reverted", data 0x) — every buyback was refused. The simulated
    limit is never below the block-capped default; a larger given one is kept."""
    for given, expected in ((120_000, simulation._DEFAULT_TX_GAS),
                            (20_000_000, 20_000_000)):
        node = _Node([{"calls": [_ok(ETH), _ok(1000), _tx_entry(),
                                 _ok(ETH - GAS_USED * BASE_FEE), _ok(750)]}])
        tx = dict(_tx(), gas=given, maxFeePerGas=3 * BASE_FEE)
        simulation.simulate(tx, holder=HOLDER, chain="base", tokens=[USDC],
                            spenders=[], rpc=node)
        the_tx = node.bundle()["blockStateCalls"][0]["calls"][2]
        assert int(the_tx["gas"], 16) == expected
        assert int(the_tx["maxFeePerGas"], 16) == 3 * BASE_FEE


def test_a_tipped_transaction_simulates_at_tip_zero():
    """085 reval: an ArbOS chain (robinhood) charges no tip, and the rail sends a
    1e8 tip there (its eth_maxPriorityFeePerGas answers 0). Adding back
    gasUsed x (base + tip) read as a native INFLOW above the dust bound and refused
    every ERC-20 swap. The simulated tip is 0, so the charge is gasUsed x base fee."""
    node = _Node([{"calls": [_ok(ETH), _ok(1000), _tx_entry(),
                             _ok(ETH - GAS_USED * BASE_FEE), _ok(750)]}])
    tx = dict(_tx(), maxPriorityFeePerGas=10 ** 8, maxFeePerGas=3 * BASE_FEE)
    d = simulation.simulate(tx, holder=HOLDER, chain="robinhood", tokens=[USDC],
                            spenders=[], rpc=node)
    assert d.ok is True
    assert d.native_delta == 0
    the_tx = node.bundle()["blockStateCalls"][0]["calls"][2]
    assert int(the_tx["maxPriorityFeePerGas"], 16) == 0
    assert int(the_tx["maxFeePerGas"], 16) == 3 * BASE_FEE
