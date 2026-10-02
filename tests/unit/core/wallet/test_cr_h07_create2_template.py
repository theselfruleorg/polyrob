"""CR-H07: CREATE2 strands a msg.sender-minted supply on the factory.

The fixed-supply template mints to msg.sender; through the Arachnid factory
that is the factory. deploy_token refuses salt/vanity, the guard refuses a
template on the CREATE2 path, refuses any constructor event naming the factory,
and on the CREATE path asserts the holder receives exactly supply_raw.
"""
import pytest

from core.wallet import deploy_guard, token_template
from core.wallet.simulation import Deltas
from tests.unit.tools.defi.test_deploy_and_call_verbs import _armed, _tool  # noqa: F401
from tools.defi.trade_tool import DeployTokenParams

HOLDER = "0x2222222222222222222222222222222222222222"
T_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
T_OWNERSHIP = "0x8be0079c531659141344cd1fd0a4f28419497f9722a3daafe3b4186f6b6457e0"
ZERO = "0x" + "00" * 32


def _w(addr):
    return "0x" + addr[2:].lower().rjust(64, "0")


class _Intent:
    def __init__(self, **kw):
        self.init_code = kw.get("init_code", "0x6080")
        self.expected_runtime = kw.get("expected_runtime")
        self.immutable_slots = kw.get("immutable_slots", ())
        self.create2_salt = kw.get("create2_salt")
        self.expected_holder_mint_raw = kw.get("expected_holder_mint_raw")


def _c2_deltas(address, **kw):
    base = dict(ok=True, native_delta=0, token_deltas={}, allowance_deltas={},
                gas_used=500_000, return_data="0x" + "00" * 12 + address[2:].lower())
    base.update(kw)
    return Deltas(**base)


@pytest.mark.asyncio
async def test_deploy_token_refuses_a_vanity_address():
    tool, _ = _tool()
    res = await tool.deploy_token(DeployTokenParams(
        chain="base", name="Rob Coin", symbol="ROB", supply=1_000,
        max_spend_usd=5.0, vanity="ab", dry_run=True))
    assert res.error and "CREATE2 factory" in res.error


@pytest.mark.asyncio
async def test_deploy_token_declares_the_holder_mint():
    captured = []
    tool, _ = _tool(captured=captured)
    res = await tool.deploy_token(DeployTokenParams(
        chain="base", name="Rob Coin", symbol="ROB", supply=1_000,
        max_spend_usd=5.0, dry_run=True))
    assert res.error is None, res.error
    assert captured[0].expected_holder_mint_raw == 1_000 * 10 ** 18
    assert captured[0].create2_salt is None


def test_guard_refuses_the_template_on_the_create2_path():
    salt = "0x" + "11" * 32
    intent = _Intent(init_code=token_template.INIT_CODE,
                     expected_runtime=token_template.RUNTIME, create2_salt=salt)
    addr = deploy_guard.predict_create2_address(salt, token_template.INIT_CODE)
    facts, why = deploy_guard.assert_deploy(intent, _c2_deltas(addr),
                                            holder=HOLDER, nonce=None)
    assert facts is None and "CREATE2 factory" in why


def test_create2_facts_never_claim_a_template_match():
    salt = "0x" + "11" * 32
    addr = deploy_guard.predict_create2_address(salt, "0x6080")
    facts, why = deploy_guard.assert_deploy(_Intent(create2_salt=salt),
                                            _c2_deltas(addr), holder=HOLDER, nonce=None)
    assert why is None and facts.template_matched is False


def test_create2_constructor_that_makes_the_factory_owner_refuses():
    salt = "0x" + "11" * 32
    addr = deploy_guard.predict_create2_address(salt, "0x6080")
    log = {"address": addr, "topics": [T_OWNERSHIP, ZERO, _w(deploy_guard.CREATE2_FACTORY)],
           "data": "0x"}
    facts, why = deploy_guard.assert_deploy(
        _Intent(create2_salt=salt), _c2_deltas(addr, logs=(log,)),
        holder=HOLDER, nonce=None)
    assert facts is None and "names the CREATE2 factory" in why


def _create_deltas(minted_to, amount, token):
    log = {"address": token, "topics": [T_TRANSFER, ZERO, _w(minted_to)],
           "data": "0x" + f"{amount:064x}"}
    return Deltas(ok=True, native_delta=0, gas_used=500_000,
                  return_data="0x6080604052", logs=(log,))


def test_create_path_asserts_the_holder_receives_the_supply():
    addr = deploy_guard.predict_create_address(HOLDER, 7)
    intent = _Intent(expected_holder_mint_raw=1000)
    facts, why = deploy_guard.assert_deploy(
        intent, _create_deltas(HOLDER, 1000, addr), holder=HOLDER, nonce=7)
    assert why is None, why
    facts, why = deploy_guard.assert_deploy(
        intent, _create_deltas("0x" + "99" * 20, 1000, addr), holder=HOLDER, nonce=7)
    assert facts is None and "declared supply" in why
    facts, why = deploy_guard.assert_deploy(
        intent, _create_deltas(HOLDER, 999, addr), holder=HOLDER, nonce=7)
    assert facts is None
