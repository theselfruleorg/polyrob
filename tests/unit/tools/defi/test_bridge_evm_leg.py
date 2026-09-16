"""The EVM-origin bridge leg (039 Unit B2).

Every test here is about the same question: does the transaction we SIGN match the
order we PRICED? Relay decides the path; it does not decide what leaves the wallet.
"""
import pytest

from tools.defi.bridge_evm_leg import (EvmOriginLeg, PreparedLeg,
                                       assert_order_matches_request)

RELAY_DEPOSITORY = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
HOLDER = "0x2222222222222222222222222222222222222222"
BASE = 8453
AMOUNT = 3 * 10 ** 16          # 0.03 ETH — the owner's real Base -> Robinhood leg


def _tx_data(**kw):
    base = {"to": RELAY_DEPOSITORY, "data": "0xdeadbeef",
            "value": str(AMOUNT), "chainId": BASE}
    base.update(kw)
    return base


# --------------------------------------------------------------------------
# The signable item must match the request
# --------------------------------------------------------------------------

def test_a_matching_order_passes():
    assert assert_order_matches_request(
        _tx_data(), origin_chain_id=BASE, amount_in_raw=AMOUNT) is None


def test_a_different_value_refuses():
    """The single most expensive mismatch: the quote describes our order, the
    transaction spends something else."""
    problem = assert_order_matches_request(
        _tx_data(value=str(AMOUNT * 10)), origin_chain_id=BASE, amount_in_raw=AMOUNT)
    assert problem and "not the declared" in problem


def test_a_different_chain_refuses():
    problem = assert_order_matches_request(
        _tx_data(chainId=1), origin_chain_id=BASE, amount_in_raw=AMOUNT)
    assert problem and "chain 1" in problem


def test_an_absent_chain_id_refuses():
    d = _tx_data()
    d.pop("chainId")
    problem = assert_order_matches_request(d, origin_chain_id=BASE, amount_in_raw=AMOUNT)
    assert problem and "chain id" in problem


def test_an_unreadable_value_refuses_rather_than_defaulting_to_zero():
    problem = assert_order_matches_request(
        _tx_data(value="not-a-number"), origin_chain_id=BASE, amount_in_raw=AMOUNT)
    assert problem and "no native value" in problem


def test_a_missing_destination_refuses():
    problem = assert_order_matches_request(
        _tx_data(to=""), origin_chain_id=BASE, amount_in_raw=AMOUNT)
    assert problem and "no usable EVM destination" in problem


def test_non_hex_calldata_refuses():
    problem = assert_order_matches_request(
        _tx_data(data="drop table"), origin_chain_id=BASE, amount_in_raw=AMOUNT)
    assert problem and "0x-prefixed" in problem


# --------------------------------------------------------------------------
# prepare(): build -> declare native -> simulate -> size
# --------------------------------------------------------------------------

class _Receipt:
    def __init__(self, status="success", block_number=42):
        self.status = status
        self.block_number = block_number
        self.gas_used = 90_000

    @property
    def succeeded(self):
        return self.status == "success"


class _FakeRail:
    """Stands in for EvmRail. Records what it was asked to build and send."""

    def __init__(self, chain=None, signer=None, **_kw):
        self.chain = chain
        self.built = None
        self.sent = None
        self.sized_with = None
        self.receipt = _Receipt()
        self.send_error = None

    def build_call(self, *, to, data, value):
        self.built = {"to": to, "data": data, "value": value,
                      "gas": 120_000, "chainId": BASE}
        return dict(self.built)

    def size_gas(self, tx, sim_gas_used):
        self.sized_with = sim_gas_used
        return {**tx, "gas": sim_gas_used * 3 // 2}

    def sign_and_send(self, tx):
        if self.send_error:
            raise RuntimeError(self.send_error)
        self.sent = tx
        return "0xfeed"

    def await_receipt(self, tx_hash, **_kw):
        return self.receipt


class _Signer:
    address = HOLDER


class _Decision:
    def __init__(self, allowed=True, reason="authorized", lane="autonomous",
                 amount_usd=60.0, sim_gas_used=100_000):
        self.allowed = allowed
        self.reason = reason
        self.lane = lane
        self.amount_usd = amount_usd
        self.sim_gas_used = sim_gas_used


class _Tool:
    def _price(self, chain, addr):
        return 2000.0

    def _fallback_price(self, chain, addr):
        return None


def _leg(rail=None, decision=None, captured=None):
    rail = rail or _FakeRail()

    def _factory(**kw):
        rail.chain = kw.get("chain")
        return rail

    def _authorize(intent, tx, **kw):
        if captured is not None:
            captured["intent"] = intent
            captured["tx"] = tx
            captured["kw"] = kw
        return decision or _Decision()

    return EvmOriginLeg(chain="base", signer=_Signer(),
                        rail_factory=_factory, authorize_fn=_authorize), rail


def _prepare(leg, *, amount_usd=60.0, gate=None):
    return leg.prepare(tx_data=_tx_data(), origin_chain_id=BASE,
                       amount_in_raw=AMOUNT, amount_usd=amount_usd,
                       gate=gate, execution_context=None, tool=_Tool(),
                       idempotency_key="bridge-1")


def test_prepare_declares_a_NATIVE_intent():
    """`token=None` is the whole point: it is what makes the guard assert the
    native outflow instead of refusing it as an undeclared native move."""
    captured = {}
    leg, _rail = _leg(captured=captured)
    prepared = _prepare(leg)
    assert prepared.ok is True
    intent = captured["intent"]
    assert intent.token is None
    assert intent.chain == "base"
    assert intent.amount_raw == AMOUNT
    assert intent.to == RELAY_DEPOSITORY


def test_prepare_builds_a_value_carrying_call():
    leg, rail = _leg()
    _prepare(leg)
    assert rail.built["value"] == AMOUNT
    assert rail.built["to"] == RELAY_DEPOSITORY
    assert rail.built["data"] == "0xdeadbeef"


def test_prepare_sizes_gas_from_the_simulation():
    leg, rail = _leg()
    prepared = _prepare(leg)
    assert rail.sized_with == 100_000
    assert prepared.tx["gas"] == 150_000


def test_prepare_refuses_when_the_guard_refuses():
    leg, _rail = _leg(decision=_Decision(allowed=False, reason="refused: nope",
                                         lane="refuse"))
    prepared = _prepare(leg)
    assert prepared.ok is False
    assert "nope" in prepared.reason


def test_an_owner_queue_lane_is_not_a_refusal():
    """Above the autonomous ceiling the guard says 'ask', not 'no'. Collapsing the
    two would make every large bridge a dead end."""
    leg, _rail = _leg(decision=_Decision(allowed=False, reason="owner approval required",
                                         lane="owner_queue"))
    prepared = _prepare(leg)
    assert prepared.ok is True
    assert prepared.needs_owner_approval is True
    assert prepared.tx is not None


def test_a_within_ceiling_leg_needs_no_owner_approval():
    leg, _rail = _leg()
    prepared = _prepare(leg)
    assert prepared.needs_owner_approval is False


def test_prepare_refuses_a_mismatched_order_before_building_anything():
    leg, rail = _leg()
    prepared = leg.prepare(tx_data=_tx_data(value="1"), origin_chain_id=BASE,
                           amount_in_raw=AMOUNT, amount_usd=60.0, gate=None,
                           execution_context=None, tool=_Tool(),
                           idempotency_key="bridge-1")
    assert prepared.ok is False
    assert rail.built is None, "nothing may be built from an order we rejected"


def test_prepare_reports_a_read_only_chain_as_a_reason_not_a_crash():
    def _boom(**_kw):
        raise ValueError("chain 'solana' is read-only here")
    leg = EvmOriginLeg(chain="solana", signer=_Signer(), rail_factory=_boom,
                       authorize_fn=lambda *a, **k: _Decision())
    prepared = _prepare(leg)
    assert prepared.ok is False
    assert "read-only" in prepared.reason


# --------------------------------------------------------------------------
# send()/confirm(): a pending receipt is never a failure
# --------------------------------------------------------------------------

def test_send_returns_the_hash():
    leg, rail = _leg()
    prepared = _prepare(leg)
    sent = EvmOriginLeg.send(prepared)
    assert sent.state == "sent"
    assert sent.tx_hash == "0xfeed"
    assert rail.sent["gas"] == 150_000


def test_a_broadcast_failure_is_an_error_not_a_silent_pass():
    rail = _FakeRail()
    rail.send_error = "nonce too low"
    leg, _ = _leg(rail=rail)
    prepared = _prepare(leg)
    sent = EvmOriginLeg.send(prepared)
    assert sent.state == "error"
    assert "nonce too low" in sent.detail
    assert sent.tx_hash is None


def test_a_successful_receipt_confirms():
    leg, _rail = _leg()
    prepared = _prepare(leg)
    assert EvmOriginLeg.confirm(prepared, "0xfeed").state == "confirmed"


def test_a_status_zero_receipt_is_reverted_and_terminal():
    rail = _FakeRail()
    rail.receipt = _Receipt(status="failed")
    leg, _ = _leg(rail=rail)
    prepared = _prepare(leg)
    assert EvmOriginLeg.confirm(prepared, "0xfeed").state == "reverted"


def test_no_receipt_is_pending_and_carries_the_hash():
    """Never 'failed'. The origin may still land, and a re-sent bridge pays twice."""
    rail = _FakeRail()
    rail.receipt = _Receipt(status="pending", block_number=None)
    leg, _ = _leg(rail=rail)
    prepared = _prepare(leg)
    out = EvmOriginLeg.confirm(prepared, "0xfeed")
    assert out.state == "pending"
    assert out.tx_hash == "0xfeed"


def test_a_receipt_read_error_is_pending_not_failed():
    class _Boom(_FakeRail):
        def await_receipt(self, tx_hash, **_kw):
            raise RuntimeError("rpc down")
    leg, _ = _leg(rail=_Boom())
    prepared = _prepare(leg)
    out = EvmOriginLeg.confirm(prepared, "0xfeed")
    assert out.state == "pending"
    assert out.tx_hash == "0xfeed"


def test_every_refusal_carries_its_reason_in_the_header():
    """The caller prints the header and nothing else, so a verdict must appear in
    it exactly once. On the first prod dry run the PolicyGate refusal printed
    twice — once from the header and once appended — which reads like two
    different problems."""
    leg, _rail = _leg()
    bad = leg.prepare(tx_data=_tx_data(value="1"), origin_chain_id=BASE,
                      amount_in_raw=AMOUNT, amount_usd=60.0, gate=None,
                      execution_context=None, tool=_Tool(),
                      idempotency_key="k")
    assert bad.ok is False
    assert "not the declared" in bad.header
    assert bad.header.count("guard:") == 1

    refused, _ = _leg(decision=_Decision(allowed=False, reason="refused: nope",
                                         lane="refuse"))
    out = _prepare(refused)
    assert out.header.count("guard:") == 1
    assert "nope" in out.header


def test_a_read_only_chain_reason_reaches_the_header_too():
    def _boom(**_kw):
        raise ValueError("chain 'solana' is read-only here")
    leg = EvmOriginLeg(chain="solana", signer=_Signer(), rail_factory=_boom,
                       authorize_fn=lambda *a, **k: _Decision())
    out = _prepare(leg)
    assert out.ok is False and "read-only" in out.header


# --- the guard must not refuse itself on oracle drift ------------------------------

def test_the_declared_ceiling_absorbs_price_source_disagreement():
    """Live on prod 2026-09-12: the SAME bridge refused at 15:16 and passed at
    15:20. `max_spend_usd` was the provider's amountUsd while tx_guard prices the
    outflow through its OWN feed, so the verdict turned on which oracle was
    momentarily higher. The agent's summary: "bridge guard self-refuses at any
    amount"."""
    captured = {}
    leg, _rail = _leg(captured=captured)
    _prepare(leg, amount_usd=91.42)
    declared = captured["intent"].max_spend_usd
    assert declared > 91.42, "no headroom means a coin flip between two oracles"
    # a guard valuation 0.027% above the quote — the measured prod case — fits
    assert declared >= 91.42 * 1.00027
    # and the headroom stays small: this is drift tolerance, not spending room
    assert declared <= 91.42 * 1.02


def test_an_unvalued_quote_still_declares_zero():
    """No USD value must NOT become 'unbounded' by multiplying through."""
    captured = {}
    leg, _rail = _leg(captured=captured)
    _prepare(leg, amount_usd=None)
    assert captured["intent"].max_spend_usd == 0.0


def test_the_tolerance_cannot_wave_through_a_materially_bigger_move():
    """The declared figure exists to catch a transaction moving materially more
    than quoted. 1% must stay far below anything that matters."""
    from tools.defi.bridge_evm_leg import VALUATION_TOLERANCE
    assert 0 < VALUATION_TOLERANCE <= 0.01
