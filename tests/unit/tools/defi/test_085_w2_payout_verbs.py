"""085 W2 at the verbs: what each money verb hands the guard for a payout shape.

``defi_trade_call`` (payout_shape, the decoded ERC-20 payee, address poisoning,
the CR-H02 grant rule), ``deploy_contract`` (payout_shape + the endowment proof),
the dapp bridge (a page's native send to a non-contract), ``nft_transfer``
(poisoning), the lookalike set (our own addresses), and 085 W8 txguard LEAD-3
(``send_anyway`` counts only from an owner seat).
"""
import contextlib
import json
from types import SimpleNamespace

import pytest

from core.wallet import address_lookalike
from core.wallet.tx_guard import Decision
from tools.defi.trade_tool import CallParams, DefiTradeTool, DeployContractParams

HOLDER = "0x2222222222222222222222222222222222222222"
TOKEN = "0x4444444444444444444444444444444444444444"
TOKEN_OUT = "0x5555555555555555555555555555555555555555"
PAYEE = "0x7777777777777777777777777777777777777777"
TARGET = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"


class _Gate:
    def __init__(self):
        self.recorded = []

    @contextlib.asynccontextmanager
    async def reserve(self):
        yield

    def record(self, **kw):
        self.recorded.append(kw)


class _Signer:
    address = HOLDER


class _Wallet:
    def __init__(self, gate):
        self.policy = gate

    def operational_signer(self):
        return _Signer()


class _Rail:
    def __init__(self, chain, signer, **kw):
        pass

    def build_deploy(self, *, init_code, value=0):
        return {"to": None, "data": init_code, "value": value, "nonce": 4}

    def build_call(self, *, to, data, value=0):
        return {"to": to, "data": data, "value": value, "nonce": 4}

    def build_native_transfer(self, *, to, amount_wei):
        return {"to": to, "data": "0x", "value": amount_wei, "nonce": 4}

    def build_erc20_transfer(self, *, token, to, amount_raw):
        return {"to": token, "data": "0xa9059cbb", "value": 0, "nonce": 4}

    def size_gas(self, tx, sim_gas_used):
        return tx


def _tool(captured):
    def _guard(intent, tx, **kw):
        captured.append(intent)
        return Decision(allowed=False, reason="test", lane="refuse")
    return DefiTradeTool(wallet=_Wallet(_Gate()), rail_factory=_Rail, guard_fn=_guard,
                         price_fn=lambda c, a: 1.0, fallback_price_fn=lambda c, a: None)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DEFI_DEPLOY_ENABLED", "true")
    monkeypatch.setenv("DEFI_CALL_ENABLED", "true")
    monkeypatch.setattr(address_lookalike, "paid_counterparties", lambda **k: [])
    monkeypatch.setattr(address_lookalike, "own_addresses", lambda: [])
    monkeypatch.setattr("tools.defi.call_verb.typed_verb_only_refusal", lambda to: None)


def _transfer_data(to, amount):
    return "0xa9059cbb" + "0" * 24 + to[2:].lower() + hex(amount)[2:].rjust(64, "0")


# -- defi_trade_call ---------------------------------------------------------

@pytest.mark.asyncio
async def test_erc20_payout_call_is_payout_shaped_and_bound_to_the_payee():
    captured = []
    await _tool(captured).call(CallParams(
        chain="base", to=TOKEN, calldata=_transfer_data(PAYEE, 20_000_000),
        spend_token=TOKEN, spend_max_raw=20_000_000, max_spend_usd=25.0))
    intent = captured[0]
    assert intent.payout_shape is True
    assert intent.to.lower() == PAYEE.lower()       # the 6b binding runs on the payee


@pytest.mark.asyncio
async def test_native_payout_call_is_payout_shaped():
    captured = []
    await _tool(captured).call(CallParams(
        chain="base", to=PAYEE, calldata="0xdeadbeef", value=0.005, max_spend_usd=25.0))
    assert captured[0].payout_shape is True


@pytest.mark.asyncio
async def test_inflow_declared_call_is_not_payout_shaped():
    captured = []
    await _tool(captured).call(CallParams(       # $1 price: 0.004 native out, 9 USDC back
        chain="base", to=TARGET, calldata="0x12345678" + "00" * 32, value=0.004,
        receive_token="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        receive_min_raw=9_000_000, max_spend_usd=25.0))
    intent = captured[0]
    assert intent.payout_shape is False
    assert intent.to.lower() == TARGET.lower()


@pytest.mark.asyncio
async def test_a_lookalike_payee_refuses_before_the_guard(monkeypatch):
    real = "0x7777aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa7777"
    monkeypatch.setattr(address_lookalike, "paid_counterparties", lambda **k: [real])
    captured = []
    res = await _tool(captured).call(CallParams(
        chain="base", to=TOKEN, calldata=_transfer_data(PAYEE, 1),
        spend_token=TOKEN, spend_max_raw=1, max_spend_usd=1.0))
    assert "ADDRESS-POISONING" in (res.error or "")
    assert captured == []


@pytest.mark.asyncio
async def test_a_grant_to_an_unpinned_spender_applies_the_owner_turn_rule(monkeypatch):
    monkeypatch.setattr("core.wallet.tx_guard.pinned_route_spenders", lambda chain: set())
    tool = _tool(captured := [])
    monkeypatch.setattr(tool, "_non_route_grant_refusal",
                        lambda ctx, spender: f"refused: {spender} is not pinned")
    res = await tool.call(CallParams(
        chain="base", to=TARGET, calldata="0x12345678" + "00" * 32,
        spend_token=TOKEN, spend_max_raw=1, allow_spender=PAYEE, allow_max_raw=1,
        receive_token=TOKEN_OUT, receive_min_raw=1, max_spend_usd=1.0))
    assert "not pinned" in (res.error or "")
    assert captured == []


def test_erc20_payee_decoder():
    from tools.defi.call_verb import _erc20_payee
    raw = bytes.fromhex(_transfer_data(PAYEE, 5)[2:])
    assert _erc20_payee(raw) == PAYEE.lower()
    tf = bytes.fromhex("23b872dd" + "0" * 24 + HOLDER[2:] + "0" * 24 + PAYEE[2:] + "0" * 64)
    assert _erc20_payee(tf) == PAYEE.lower()
    assert _erc20_payee(bytes.fromhex("deadbeef")) is None


# -- deploy_contract ---------------------------------------------------------

@pytest.mark.asyncio
async def test_endowed_deploy_is_payout_shaped_and_a_plain_one_is_not():
    captured = []
    tool = _tool(captured)
    code = "0x6080604052"
    await tool.deploy_contract(DeployContractParams(
        chain="base", bytecode=code, value=0.01, max_spend_usd=50.0))
    await tool.deploy_contract(DeployContractParams(
        chain="base", bytecode=code, max_spend_usd=50.0))
    assert [i.payout_shape for i in captured] == [True, False]


def _deploy_deltas(landed):
    from core.wallet.deploy_guard import predict_create_address
    from core.wallet.simulation import Deltas
    addr = predict_create_address(HOLDER, 4)
    watched = {} if landed is None else {addr.lower(): landed}
    return Deltas(ok=True, native_delta=-10 ** 16, return_data="0x6080",
                  watched_native_deltas=watched)


def _deploy_intent():
    from core.wallet.tx_guard import TxIntent
    return TxIntent(chain="base", token=None, to=None, amount_raw=10 ** 16,
                    max_spend_usd=50.0, idempotency_key="k", is_deploy=True,
                    init_code="0x6080604052", payout_shape=True)


def test_a_constructor_that_forwards_its_endowment_refuses():
    from core.wallet.deploy_guard import assert_deploy
    facts, why = assert_deploy(_deploy_intent(), _deploy_deltas(0), holder=HOLDER, nonce=4)
    assert facts is None and "forwards value" in why


def test_an_unmeasured_endowment_refuses():
    from core.wallet.deploy_guard import assert_deploy
    facts, why = assert_deploy(_deploy_intent(), _deploy_deltas(None), holder=HOLDER, nonce=4)
    assert facts is None and "cannot be proven" in why


def test_an_endowment_that_stays_is_a_deployment():
    from core.wallet.deploy_guard import assert_deploy
    facts, why = assert_deploy(_deploy_intent(), _deploy_deltas(10 ** 16), holder=HOLDER,
                               nonce=4)
    assert why is None and facts is not None


def test_the_guard_measures_the_new_address_for_an_endowed_deploy():
    from core.wallet import tx_guard
    from core.wallet.deploy_guard import predict_create_address
    from core.wallet.tx_guard_steps.context import GuardContext
    from core.wallet.tx_guard_steps.measure import step_5_simulate
    seen = {}

    def sim(**kw):
        seen.update(kw)
        return _deploy_deltas(10 ** 16)

    tx = {"to": None, "data": "0x6080604052", "value": 10 ** 16, "nonce": 4}
    c = GuardContext(intent=_deploy_intent(), tx=tx, holder=HOLDER, gate=None,
                     execution_context=None, tool_self=None, simulate_fn=sim,
                     price_fn=None, fallback_price_fn=None, rpc_is_pinned_fn=None,
                     halted_fn=None, entry_paused_fn=None, forged_fn=None,
                     autonomous_ok_fn=None, liquidity_rpc=None, account_rpc=None,
                     outer_tx=tx)
    import unittest.mock as m
    with m.patch.object(tx_guard, "_simulated_target_refusal", lambda *a: None):
        assert step_5_simulate(c) is None
    assert seen["watch_native"] == (predict_create_address(HOLDER, 4),)


def test_simulation_measures_watched_native_balances():
    from core.wallet import simulation
    addr = "0x" + "cd" * 20

    def rpc(method, params, **kw):
        if method == "eth_getBlockByNumber":
            return {"baseFeePerGas": "0x1", "gasLimit": hex(30_000_000)}
        if method == "eth_getBalance":
            return hex(10 ** 18)
        calls = params[0]["blockStateCalls"][0]["calls"]
        out = []
        tx_index = (len(calls) - 1) // 2
        for i, call in enumerate(calls):
            if i == tx_index:
                out.append({"status": "0x1", "logs": [], "returnData": "0x", "gasUsed": "0x1"})
                continue
            after = i > tx_index
            who = call["data"][-40:]
            if who == addr[2:]:
                val = 10 ** 16 if after else 0
            else:
                val = 10 ** 18 - (10 ** 16 if after else 0)
            out.append({"status": "0x1", "returnData": hex(val)})
        return [{"calls": out}]

    d = simulation.simulate({"to": None, "data": "0x60", "value": 10 ** 16}, holder=HOLDER,
                            chain="base", tokens=[], spenders=[], rpc=rpc,
                            watch_native=(addr,))
    assert d.ok, d.error
    assert d.watched_native_deltas == {addr: 10 ** 16}


# -- the dapp bridge (money L-4) ---------------------------------------------

@pytest.mark.asyncio
async def test_a_page_native_send_to_an_allowlisted_eoa_is_payout_shaped():
    from tools.dapp_browser import bridge as B
    captured = []

    def _guard(intent, tx, **kw):
        captured.append(intent)
        return Decision(allowed=False, reason="test", lane="refuse")

    envelope = B.Envelope(chain="base", max_spend_usd=50.0, session_budget_usd=100.0,
                          allow_contracts=(PAYEE,), approval_timeout_sec=0.5)
    bridge = B.WalletBridge(
        envelope=envelope, wallet=_Wallet(_Gate()), execution_context=None,
        rail_factory=_Rail, guard_fn=_guard, price_fn=lambda c, a: 2500.0,
        rpc_fn=lambda chain, method, params: "0x" if method == "eth_getCode" else "0x1",
        armed_origin="https://app.example")
    page = SimpleNamespace(main_frame=SimpleNamespace(url="https://app.example"),
                           on=lambda *a: None)
    await bridge.handle({"page": page, "frame": page.main_frame, "context": None},
                        json.dumps({"method": "eth_sendTransaction",
                                    "params": [{"to": PAYEE, "data": "0x", "value": "0x1"}]}))
    assert captured and captured[0].payout_shape is True


# -- the lookalike set: our own addresses (evmverbs L1) ----------------------

def test_a_lookalike_of_an_owner_address_refuses(monkeypatch):
    monkeypatch.undo()
    owner = "0x45dd0000000000000000000000000000000beef1"
    fake = "0x45d40000000000000000000000000000000beef1"
    monkeypatch.setenv("OWNER_WALLET_ADDRESSES", owner)
    monkeypatch.setattr(address_lookalike, "paid_counterparties", lambda **k: [])
    monkeypatch.setattr("core.wallet.own_addresses.agent_wallet_addresses", lambda: [])
    assert "ADDRESS-POISONING" in (address_lookalike.poisoning_refusal(fake) or "")
    assert address_lookalike.poisoning_refusal(owner) is None


@pytest.mark.asyncio
async def test_nft_transfer_runs_the_poisoning_check(monkeypatch):
    from tools.defi.trade_params import NftTransferParams
    monkeypatch.setattr("tools.defi.trade_tool._nft_tools_enabled", lambda: True)
    monkeypatch.setattr(address_lookalike, "poisoning_refusal",
                        lambda to, **k: "refused: ADDRESS-POISONING lookalike")
    res = await _tool([]).nft_transfer(NftTransferParams(
        chain="base", contract=TOKEN, standard="erc721", token_id=1, to=PAYEE,
        max_spend_usd=1.0))
    assert "ADDRESS-POISONING" in (res.error or "")


# -- 085 W8 txguard LEAD-3: send_anyway only from the owner's typed word -----

def test_send_anyway_counts_only_from_an_owner_seat():
    from core.security.turn_facts import owner_seat_context
    from tools.defi.transfer_verb import owner_typed_anyway
    params = SimpleNamespace(send_anyway=True)
    model_ctx = SimpleNamespace(user_id="u", role="orchestrator", is_sub_agent=False,
                                metadata={"turn_kind": None})
    assert owner_typed_anyway(params, model_ctx) is False
    assert owner_typed_anyway(params, None) is False
    assert owner_typed_anyway(params, owner_seat_context("u", seat="telegram")) is True
    assert owner_typed_anyway(SimpleNamespace(send_anyway=False),
                              owner_seat_context("u", seat="telegram")) is False


@pytest.mark.asyncio
async def test_a_model_send_anyway_never_reaches_the_guard_as_value_ack():
    from core.security.turn_facts import owner_seat_context
    from tools.defi.trade_tool import TransferParams
    captured = []
    tool = _tool(captured)
    p = TransferParams(chain="base", token="native", to=PAYEE, amount=0.001,
                       max_spend_usd=5.0, send_anyway=True)
    model_ctx = SimpleNamespace(user_id="u", role="orchestrator", is_sub_agent=False,
                                session_id="s", metadata={"turn_kind": None})
    await tool.transfer(p, model_ctx)
    await tool.transfer(p, owner_seat_context("u", seat="telegram"))
    assert [i.value_ack for i in captured] == [False, True]
