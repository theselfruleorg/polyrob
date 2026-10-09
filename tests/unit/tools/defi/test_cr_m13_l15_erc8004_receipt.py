"""CR-M13 + CR-L15 (2026-09-23): the ERC-8004 identity record.

M13: the agentId was saved from the SIMULATION. A concurrent registration by
anyone else takes that id first, and the served file then claims another
agent's token as `verified`. The id now comes from the receipt's
Transfer(0x0 -> holder) log, and a missing or different id writes nothing.

L15: the "already registered" chain read ran OUTSIDE the reservation and could
not see a registration still in flight; a transferred-in token was described
as the agent's own identity.
"""
import time
import types

import pytest

from core.wallet import erc8004
from core.wallet.broadcast.evm import Receipt
from core.wallet.simulation import _TOPIC_TRANSFER
from core.wallet.tx_guard import Decision
from tests.unit.tools.defi import test_trade_t4 as t4
from tools.defi import agent_registration as ar
from tools.defi.trade_tool import DefiTradeTool, RegisterAgentParams

HOLDER = t4._Signer.address
REG = erc8004.resolve_identity_registry("base")
ZERO_WORD = "0x" + "0" * 64


def _word(a):
    return "0x" + a[2:].lower().rjust(64, "0")


def _mint_log(agent_id, to=HOLDER, registry=REG):
    return {"address": registry, "topics": [_TOPIC_TRANSFER, ZERO_WORD, _word(to),
                                            hex(agent_id)], "data": "0x"}


class _Gate(t4._Gate):
    def __init__(self, audit=()):
        super().__init__()
        self.audit_log = list(audit)


def _tool(receipt_logs, *, sim_id=7, audit=(), existing=None, monkeypatch=None):
    gate = _Gate(audit)
    saved = []

    class R(t4._Rail):
        def await_receipt(self, tx_hash, **kw):
            return Receipt(tx_hash=tx_hash, status="success", block_number=1)

        def _rpc(self, method, params, *a, **k):
            return {"logs": receipt_logs}

    def guard(intent, tx, **kw):
        return Decision(allowed=True, reason="test", lane="autonomous",
                        amount_usd=0.5, agent_id=sim_id)

    tool = DefiTradeTool(wallet=t4._Wallet(gate), rail_factory=R, guard_fn=guard,
                         price_fn=lambda *a: 1.0)
    tool._notify_tx = lambda *a, **k: None
    monkeypatch.setattr(DefiTradeTool, "_read_agent_id",
                        staticmethod(lambda ar_, chain, holder: existing))
    import core.instance as inst
    monkeypatch.setattr(inst, "save_erc8004_record",
                        lambda *a, **kw: saved.append(kw))
    return tool, gate, saved


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("EIP8004_REGISTER_ENABLED", "true")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "rob")
    monkeypatch.delenv("A2A_BASE_URL", raising=False)
    yield
    t4._Rail.last = None


P = RegisterAgentParams(chain="base", dry_run=False)


@pytest.mark.asyncio
async def test_the_record_carries_the_receipt_id(monkeypatch):
    tool, gate, saved = _tool([_mint_log(7)], monkeypatch=monkeypatch)
    res = await tool.register_agent(P)
    assert res.error is None, res.error
    assert saved and saved[0]["agent_id"] == 7


@pytest.mark.asyncio
async def test_a_receipt_id_that_differs_from_the_simulation_writes_nothing(monkeypatch):
    tool, gate, saved = _tool([_mint_log(8)], sim_id=7, monkeypatch=monkeypatch)
    res = await tool.register_agent(P)
    assert not saved
    assert "NOT written" in (res.extracted_content or "")


@pytest.mark.asyncio
async def test_a_receipt_without_our_mint_writes_nothing(monkeypatch):
    tool, gate, saved = _tool([_mint_log(7, to="0x" + "5" * 40)],
                              monkeypatch=monkeypatch)
    await tool.register_agent(P)
    assert not saved


@pytest.mark.asyncio
async def test_an_in_flight_registration_refuses_a_second_one(monkeypatch):
    audit = [{"action": "register_agent", "chain": "base", "ts": time.time() - 60,
              "result_ref": "0xabc"}]
    tool, gate, saved = _tool([_mint_log(7)], audit=audit, monkeypatch=monkeypatch)
    res = await tool.register_agent(P)
    assert res.error and "already broadcast" in res.error and "0xabc" in res.error
    assert not gate.recorded


@pytest.mark.asyncio
async def test_an_old_registration_row_does_not_block(monkeypatch):
    audit = [{"action": "register_agent", "chain": "base",
              "ts": time.time() - ar.IN_FLIGHT_WINDOW_SEC - 60, "result_ref": "0xabc"}]
    tool, gate, saved = _tool([_mint_log(7)], audit=audit, monkeypatch=monkeypatch)
    res = await tool.register_agent(P)
    assert res.error is None, res.error


@pytest.mark.asyncio
async def test_the_chain_check_runs_inside_the_reservation(monkeypatch):
    tool, gate, saved = _tool([_mint_log(7)], existing=5, monkeypatch=monkeypatch)
    held = []
    real_reserve = gate.reserve

    import contextlib

    @contextlib.asynccontextmanager
    async def reserve():
        held.append(True)
        async with real_reserve():
            yield
        held.append(False)
    gate.reserve = reserve

    def read(ar_, chain, holder):
        assert held and held[-1] is True, "the chain read ran outside reserve()"
        return 5
    monkeypatch.setattr(DefiTradeTool, "_read_agent_id", staticmethod(read))
    res = await tool.register_agent(P)
    assert res.error and "already holds" in res.error


def test_a_proven_self_mint_names_the_existing_registration():
    err = ar.check_not_already_registered(existing_agent_id=42, chain="base")
    assert "42" in err and "self-minted" in err


def test_minted_id_parser_needs_exactly_one_mint_to_us():
    assert ar.minted_agent_id_from_receipt(
        {"logs": [_mint_log(3)]}, registry=REG, holder=HOLDER) == 3
    assert ar.minted_agent_id_from_receipt(
        {"logs": [_mint_log(3), _mint_log(4)]}, registry=REG, holder=HOLDER) is None
    assert ar.minted_agent_id_from_receipt(
        {"logs": [_mint_log(3, registry="0x" + "6" * 40)]},
        registry=REG, holder=HOLDER) is None
    assert ar.minted_agent_id_from_receipt(None, registry=REG, holder=HOLDER) is None
