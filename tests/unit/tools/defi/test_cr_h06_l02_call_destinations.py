"""CR-H06 (typed-verb-only destinations) + CR-L02 (stable idempotency) for the
generic contract call."""
import pytest

from tests.unit.tools.defi.test_deploy_and_call_verbs import (  # noqa: F401
    POOL, _armed, _tool)
from tools.defi.call_verb import intent_idempotency_key, typed_verb_only_refusal
from tools.defi.trade_tool import CallParams

BASE_NPM = "0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1"
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
PONS_LOCKER = "0x267444D099b10fB5Ed7c3Cc7B7c767AdcA574952"


@pytest.mark.parametrize("to", [BASE_NPM, PERMIT2, PONS_LOCKER,
                                "0xC36442b4a4522E871399CD717aBDD847Ab11FE88"])
def test_pinned_value_holders_are_refused(to):
    assert typed_verb_only_refusal(to)
    assert typed_verb_only_refusal(to.lower())


def test_an_ordinary_contract_is_not_refused():
    assert typed_verb_only_refusal(POOL) is None


@pytest.mark.asyncio
async def test_generic_call_to_the_position_manager_never_reaches_the_guard():
    captured = []
    tool, _ = _tool(captured=captured, facts=None)
    res = await tool.call(CallParams(chain="base", to=BASE_NPM,
                                     calldata="0x0c49ccbe" + "00" * 32,
                                     max_spend_usd=5.0, dry_run=True))
    assert "typed verb" in (res.error or "")
    assert captured == []


@pytest.mark.asyncio
async def test_generic_call_key_is_derived_from_the_intent_not_random():
    captured = []
    tool, _ = _tool(captured=captured, facts=None)
    for _ in range(2):
        res = await tool.call(CallParams(chain="base", to=POOL,
                                         calldata="0xdeadbeef" + "00" * 32,
                                         max_spend_usd=5.0, dry_run=True))
        assert res.error is None, res.error
    a, b = captured
    assert a.idempotency_key == b.idempotency_key  # same built tx (same nonce)
    assert "defi_call:base:" in a.idempotency_key


def test_key_components():
    base = dict(chain="base", to=POOL, data="0xdeadbeef", value_wei=0,
                tx={"nonce": 4})
    k = intent_idempotency_key("x", **base)
    assert k == intent_idempotency_key("x", **base)
    assert k != intent_idempotency_key("x", **{**base, "data": "0xdeadbeee"})
    assert k != intent_idempotency_key("x", **{**base, "value_wei": 1})
    assert k != intent_idempotency_key("x", **{**base, "tx": {"nonce": 5}})
    assert k != intent_idempotency_key("x", **{**base, "chain": "ethereum"})
