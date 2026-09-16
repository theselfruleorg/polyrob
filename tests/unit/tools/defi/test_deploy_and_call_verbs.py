"""defi_trade.deploy_token / deploy_contract / call (042).

The verbs' own gates, above the guard: the flag, the leaf refusal, the shape
checks a caller hits before anything is simulated, and the declaration the verb
hands to tx_guard.
"""
import contextlib

import pytest

from core.wallet import token_template
from core.wallet.deploy_guard import DeployFacts
from core.wallet.tx_guard import Decision
from tools.defi.trade_tool import (
    CallParams, DefiTradeTool, DeployContractParams, DeployTokenParams)

HOLDER = "0x2222222222222222222222222222222222222222"
POOL = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


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
    last = None

    def __init__(self, chain, signer, **kw):
        self.sent = False
        self.built = None
        _Rail.last = self

    def build_deploy(self, *, init_code, value=0):
        self.built = {"to": None, "data": init_code, "value": value, "nonce": 4}
        return self.built

    def build_call(self, *, to, data, value=0):
        self.built = {"to": to, "data": data, "value": value, "nonce": 4}
        return self.built

    def size_gas(self, tx, sim_gas_used):
        return {**tx, "gas": sim_gas_used * 3 // 2}

    def sign_and_send(self, tx):
        self.sent = True
        return "0x" + "ab" * 32

    def await_receipt(self, tx_hash, **kw):
        from core.wallet.broadcast.evm import Receipt
        return Receipt(tx_hash=tx_hash, status="success", block_number=7,
                       gas_used=500_000)

    def _rpc(self, method, params, **kw):
        return {"contractAddress": "0x" + "11" * 20}


def _tool(*, allow=True, captured=None, facts="default"):
    gate = _Gate()
    if facts == "default":
        facts = DeployFacts(predicted_address="0x" + "cd" * 20,
                            runtime_size=1686,
                            runtime_hash=token_template.RUNTIME_HASH,
                            template_matched=True)

    def _guard(intent, tx, **kw):
        if captured is not None:
            captured.append(intent)
        return Decision(allowed=allow, reason="test", lane="autonomous",
                        amount_usd=1.5, sim_gas_used=501_207, deploy=facts)

    return DefiTradeTool(
        wallet=_Wallet(gate), rail_factory=_Rail, guard_fn=_guard,
        price_fn=lambda c, a: 2500.0, fallback_price_fn=lambda c, a: None,
    ), gate


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setenv("DEFI_DEPLOY_ENABLED", "true")
    monkeypatch.setenv("DEFI_CALL_ENABLED", "true")


class _Leaf:
    role = "leaf"
    is_sub_agent = False
    user_id = "owner"
    metadata = {}


# ==========================================================================
# Flags
# ==========================================================================

@pytest.mark.asyncio
async def test_deploy_is_off_by_default(monkeypatch):
    monkeypatch.delenv("DEFI_DEPLOY_ENABLED", raising=False)
    tool, _ = _tool()
    res = await tool.deploy_token(DeployTokenParams(
        chain="base", name="A", symbol="A", supply=1, max_spend_usd=1.0))
    assert "DEFI_DEPLOY_ENABLED" in (res.error or "")


@pytest.mark.asyncio
async def test_call_is_off_by_default(monkeypatch):
    monkeypatch.delenv("DEFI_CALL_ENABLED", raising=False)
    tool, _ = _tool()
    res = await tool.call(CallParams(chain="base", to=POOL,
                                     calldata="0xdeadbeef", max_spend_usd=1.0))
    assert "DEFI_CALL_ENABLED" in (res.error or "")


# ==========================================================================
# deploy_token
# ==========================================================================

@pytest.mark.asyncio
async def test_deploy_token_declares_the_pinned_template_to_the_guard():
    """The whole safety argument: the caller supplies a NAME and a SUPPLY, never
    bytecode, and the guard is handed the artifact to compare against."""
    captured = []
    tool, _ = _tool(captured=captured)
    res = await tool.deploy_token(DeployTokenParams(
        chain="base", name="Rob Coin", symbol="ROB", supply=1_000_000_000,
        max_spend_usd=5.0, dry_run=True))
    assert res.error is None, res.error
    intent = captured[0]
    assert intent.is_deploy is True
    assert intent.to is None
    assert intent.token is None
    assert intent.amount_raw == 0
    assert intent.expected_runtime == token_template.RUNTIME
    assert intent.immutable_slots == token_template.IMMUTABLE_SLOTS
    assert intent.init_code.startswith(token_template.INIT_CODE)


@pytest.mark.asyncio
async def test_deploy_token_names_the_address_before_broadcasting():
    tool, _ = _tool()
    res = await tool.deploy_token(DeployTokenParams(
        chain="base", name="Rob Coin", symbol="ROB", supply=1_000,
        max_spend_usd=5.0, dry_run=True))
    assert "0x" + "cd" * 20 in res.extracted_content
    assert "MATCHED byte for byte" in res.extracted_content
    assert "DRY RUN" in res.extracted_content
    assert _Rail.last.sent is False


@pytest.mark.asyncio
async def test_a_leaf_never_deploys():
    tool, _ = _tool()
    res = await tool.deploy_token(DeployTokenParams(
        chain="base", name="A", symbol="A", supply=1, max_spend_usd=1.0),
        _Leaf())
    assert "sub-agent" in (res.error or "")


@pytest.mark.asyncio
async def test_a_name_built_to_be_misread_is_refused_before_any_rpc():
    _Rail.last = None
    tool, _ = _tool()
    res = await tool.deploy_token(DeployTokenParams(
        chain="base", name="hides​itself", symbol="A", supply=1,
        max_spend_usd=1.0))
    assert "zero-width" in (res.error or "")
    assert _Rail.last is None


@pytest.mark.asyncio
async def test_an_unknown_chain_refuses():
    tool, _ = _tool()
    res = await tool.deploy_token(DeployTokenParams(
        chain="dogecoin", name="A", symbol="A", supply=1, max_spend_usd=1.0))
    assert res.error


@pytest.mark.asyncio
async def test_a_live_deploy_reports_the_LANDED_address_not_the_prediction():
    tool, gate = _tool()
    res = await tool.deploy_token(DeployTokenParams(
        chain="base", name="Rob Coin", symbol="ROB", supply=1_000,
        max_spend_usd=5.0, dry_run=False))
    assert "DEPLOYED AND CONFIRMED" in res.extracted_content
    assert "0x" + "11" * 20 in res.extracted_content
    assert "USE THE LANDED ADDRESS" in res.extracted_content   # the prediction disagreed
    assert gate.recorded and gate.recorded[0]["action"] == "deploy_token"
    # 043 A36: the durable spend record names what actually landed, never the
    # predicted address and never a chain name in the address field.
    assert gate.recorded[0]["counterparty"] == "0x" + "11" * 20
    assert gate.recorded[0]["chain"] == "base"


@pytest.mark.asyncio
async def test_no_gas_measurement_refuses_rather_than_burning_the_fee():
    """The built default is 120k; a deployment needs several times that, so
    broadcasting unsized would out-of-gas revert and burn the whole fee."""
    gate = _Gate()

    def _guard(intent, tx, **kw):
        return Decision(allowed=True, reason="t", lane="autonomous",
                        amount_usd=1.0, sim_gas_used=None, deploy=None)

    tool = DefiTradeTool(wallet=_Wallet(gate), rail_factory=_Rail,
                         guard_fn=_guard, price_fn=lambda c, a: 2500.0)
    res = await tool.deploy_token(DeployTokenParams(
        chain="base", name="A", symbol="A", supply=1, max_spend_usd=1.0,
        dry_run=False))
    assert "out-of-gas" in (res.error or "")


# ==========================================================================
# deploy_contract
# ==========================================================================

@pytest.mark.asyncio
async def test_solidity_source_is_refused_with_the_reason():
    tool, _ = _tool()
    res = await tool.deploy_contract(DeployContractParams(
        chain="base", bytecode="contract Foo {}", max_spend_usd=1.0))
    assert "not valid hex" in (res.error or "")
    assert "not Solidity source" in (res.error or "")


@pytest.mark.asyncio
async def test_misaligned_constructor_args_refuse():
    tool, _ = _tool()
    res = await tool.deploy_contract(DeployContractParams(
        chain="base", bytecode="0x6000", constructor_args="0xabcd",
        max_spend_usd=1.0))
    assert "word-aligned" in (res.error or "")


@pytest.mark.asyncio
async def test_constructor_args_are_appended_to_the_init_code():
    captured = []
    tool, _ = _tool(captured=captured, facts=None)
    args = "11" * 32
    await tool.deploy_contract(DeployContractParams(
        chain="base", bytecode="0x6000", constructor_args=args,
        max_spend_usd=1.0))
    assert captured[0].init_code == "0x6000" + args


@pytest.mark.asyncio
async def test_caller_supplied_bytecode_declares_NO_template():
    captured = []
    tool, _ = _tool(captured=captured, facts=None)
    await tool.deploy_contract(DeployContractParams(
        chain="base", bytecode="0x6000", max_spend_usd=1.0))
    assert captured[0].expected_runtime is None


# ==========================================================================
# call
# ==========================================================================

@pytest.mark.asyncio
async def test_call_declares_both_directions_to_the_guard():
    captured = []
    tool, _ = _tool(captured=captured, facts=None)
    res = await tool.call(CallParams(
        chain="base", to=POOL, calldata="0xdeadbeef" + "00" * 32,
        spend_token=USDC, spend_max_raw=1_000_000,
        receive_token=POOL, receive_min_raw=990_000,
        max_spend_usd=5.0, dry_run=True))
    assert res.error is None, res.error
    intent = captured[0]
    assert intent.token.lower() == USDC.lower()
    assert intent.amount_raw == 1_000_000
    assert intent.inflow_token.lower() == POOL.lower()
    assert intent.min_inflow_raw == 990_000


@pytest.mark.asyncio
async def test_a_native_value_AND_a_token_outflow_is_refused_up_front():
    """The guard asserts ONE outflow against ONE declaration; a call moving both
    cannot be held to either. Said here rather than letting the guard refuse it
    later with a message about a check the caller never heard of."""
    tool, _ = _tool()
    res = await tool.call(CallParams(
        chain="base", to=POOL, calldata="0xdeadbeef", value=0.5,
        spend_token=USDC, spend_max_raw=1, max_spend_usd=5.0))
    assert "EITHER a native value OR a token outflow" in (res.error or "")


@pytest.mark.asyncio
async def test_a_minimum_with_no_receive_token_refuses():
    tool, _ = _tool()
    res = await tool.call(CallParams(
        chain="base", to=POOL, calldata="0xdeadbeef", receive_min_raw=10,
        max_spend_usd=5.0))
    assert "no receive_token" in (res.error or "")


@pytest.mark.asyncio
async def test_calldata_shorter_than_a_selector_refuses():
    tool, _ = _tool()
    res = await tool.call(CallParams(chain="base", to=POOL, calldata="0x1234",
                                     max_spend_usd=5.0))
    assert "4-byte selector" in (res.error or "")


@pytest.mark.asyncio
async def test_an_allowance_with_no_token_refuses():
    tool, _ = _tool()
    res = await tool.call(CallParams(
        chain="base", to=POOL, calldata="0xdeadbeef",
        allow_spender=POOL, allow_max_raw=5, max_spend_usd=5.0))
    assert "claim on a TOKEN" in (res.error or "")


@pytest.mark.asyncio
async def test_the_callee_is_WATCHED_as_a_spender_when_a_token_is_declared():
    """Protocols pull through the contract being called, and an OZ-4.x token
    re-emits Approval(remaining) on transferFrom — unmeasured, that reads as a
    hidden grant on every legitimate call."""
    captured = []
    tool, _ = _tool(captured=captured, facts=None)
    await tool.call(CallParams(
        chain="base", to=POOL, calldata="0xdeadbeef", spend_token=USDC,
        spend_max_raw=1, max_spend_usd=5.0))
    assert [s.lower() for s in captured[0].watch_spenders] == [POOL.lower()]


@pytest.mark.asyncio
async def test_a_leaf_never_calls():
    tool, _ = _tool()
    res = await tool.call(CallParams(chain="base", to=POOL,
                                     calldata="0xdeadbeef", max_spend_usd=1.0),
                          _Leaf())
    assert "sub-agent" in (res.error or "")


@pytest.mark.asyncio
async def test_the_create2_report_does_not_claim_a_NONCE_derived_address():
    """On the deterministic path the address is a hash of the init code, not a
    function of the wallet's nonce. Saying otherwise describes a different
    guarantee than the one that was actually checked."""
    tool, _ = _tool()
    res = await tool.deploy_token(DeployTokenParams(
        chain="base", name="Rob Coin", symbol="ROB", supply=1_000,
        max_spend_usd=5.0, vanity="ab", dry_run=True))
    assert res.error is None, res.error
    text = res.extracted_content
    assert "predicted from the wallet's nonce" not in text
    assert "DETERMINISTIC" in text
    assert "commits to the init code" in text
    assert text.count("address:") == 1
    assert "MATCHED byte for byte" not in text  # subsumed by the commitment
