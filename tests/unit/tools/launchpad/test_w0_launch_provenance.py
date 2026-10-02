"""W0: the launch/deploy verbs WRITE provenance; the Pons probe READS the factory.

A confirmed launch is recorded as ``own_launch`` so the identity gate never asks
the owner to pin the instance's own token. The Pons probe covers a launch that
happened off this rail (the prod PNL): the factory's record names our wallet as
the DEPLOYER. The creator-fee recipient does not count — anyone can name us.
"""
import pytest

from core.wallet import token_provenance as tp
from tools.launchpad import pons, pons_abi as P
from tools.launchpad.provenance import pons_probe
from tools.launchpad.tool import LaunchParams

from tests.unit.tools.launchpad.test_launchpad_tool import HOLDER, TOKEN, _tool

OTHER = "0x9999999999999999999999999999999999999999"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    tp._reset_for_tests()
    monkeypatch.setenv("LAUNCHPAD_ENABLED", "true")
    yield
    tp._reset_for_tests()


@pytest.mark.asyncio
async def test_a_confirmed_launch_records_own_launch(monkeypatch):
    tool, _ = _tool(monkeypatch)
    await tool.launchpad_launch(LaunchParams(name="R", symbol="R", max_spend_usd=5.0,
                                             dry_run=False))
    row = tp.own_token(P.CHAIN, "0x" + "11" * 20)
    assert row and row["kind"] == "launchpad_launch"


@pytest.mark.asyncio
async def test_a_launch_with_no_event_records_nothing(monkeypatch):
    tool, _ = _tool(monkeypatch)
    monkeypatch.setattr(pons, "parse_token_launched", lambda logs: None)
    await tool.launchpad_launch(LaunchParams(name="R", symbol="R", max_spend_usd=5.0,
                                             dry_run=False))
    assert tp.own_token(P.CHAIN, P.FACTORY) is None


def _factory_rpc(deployer, fee_recipient):
    def launched(rpc, token):
        return {"token": token, "deployer": deployer,
                "creatorFeeRecipient": fee_recipient, "exists": True}
    return launched


def test_the_pons_probe_trusts_the_deployer_only(monkeypatch):
    monkeypatch.setattr(tp, "own_evm_addresses", lambda data_home=None: [HOLDER])
    monkeypatch.setattr(pons, "launched_token", _factory_rpc(HOLDER.lower(), OTHER))
    assert "deployer" in pons_probe("robinhood", TOKEN, rpc=lambda m, p: None)
    monkeypatch.setattr(pons, "launched_token", _factory_rpc(OTHER, HOLDER))
    assert pons_probe("robinhood", TOKEN, rpc=lambda m, p: None) is None
    monkeypatch.setattr(pons, "launched_token", lambda rpc, token: None)
    assert pons_probe("robinhood", TOKEN, rpc=lambda m, p: None) is None
    assert pons_probe("base", TOKEN, rpc=lambda m, p: None) is None


def test_the_pons_probe_makes_no_call_without_a_known_address(monkeypatch):
    monkeypatch.setattr(tp, "own_evm_addresses", lambda data_home=None: [])

    def boom(rpc, token):
        raise AssertionError("no RPC without an address to compare")
    monkeypatch.setattr(pons, "launched_token", boom)
    assert pons_probe("robinhood", TOKEN) is None

