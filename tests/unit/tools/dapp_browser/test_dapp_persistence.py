"""A dapp session persists across a restart (043 A37).

Before this, ``_bridges`` was process-local, so after ``polyrob.service`` bounced
``dapp_status`` could only say "no dapp wallet is connected" even when the owner
had armed one and money had moved. These tests pin the durable rail: the bridge
persists after every mutation, and ``dapp_status`` reads the store back as a
read-only record when the in-memory bridge is gone.
"""
import json
import types

import pytest

from core.dapp_session_store import DappSessionStore
from tools.dapp_browser import bridge as B
from tools.dapp_browser.tool import DappBrowserTool


HOLDER = "0xcAda546f6A6ddDE31B71aB21eF63d3EBF09Fa553"
POOL = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"


class _Signer:
    address = HOLDER


class _Wallet:
    def __init__(self):
        self.policy = _Gate()

    def operational_signer(self):
        return _Signer()


class _Gate:
    def __init__(self):
        self.recorded = []

    import contextlib

    @contextlib.asynccontextmanager
    async def reserve(self):
        yield

    def record(self, **kw):
        self.recorded.append(kw)


class _Rail:
    def __init__(self, chain, signer, **kw):
        pass

    def build_call(self, *, to, data, value=0):
        return {"to": to, "data": data, "value": value, "nonce": 1}

    def size_gas(self, tx, sim_gas_used):
        return tx

    def sign_and_send(self, tx):
        return "0x" + "ee" * 32


def _ctx(session_id="sess-A", user_id="u1"):
    return types.SimpleNamespace(session_id=session_id, user_id=user_id)


def _bridge(persist_fn=None, *, ctx=None, allow=True, usd=2.0):
    from core.wallet.tx_guard import Decision

    def _guard(intent, tx, **kw):
        return Decision(allowed=allow, reason="test", lane="autonomous",
                        amount_usd=usd, sim_gas_used=None)

    env = B.Envelope(chain="base", max_spend_usd=50.0, session_budget_usd=100.0,
                     approval_timeout_sec=0.5)
    return B.WalletBridge(
        envelope=env, wallet=_Wallet(), execution_context=ctx or _ctx(),
        rail_factory=_Rail, guard_fn=_guard, price_fn=lambda c, a: 2500.0,
        rpc_fn=lambda chain, method, params: "0x", persist_fn=persist_fn)


async def _ask(bridge, method, params=None):
    raw = await bridge.handle(None, json.dumps({"method": method,
                                                "params": params or []}))
    return json.loads(raw)


# --- the bridge fires its persist hook on every mutation -------------------- #

@pytest.mark.asyncio
async def test_a_spend_fires_the_persist_hook():
    calls = []
    bridge = _bridge(persist_fn=lambda b: calls.append(b.envelope.spent_usd))
    out = await _ask(bridge, "eth_sendTransaction",
                     [{"to": POOL, "data": "0x12345678", "from": HOLDER}])
    assert out.get("result")           # it broadcast
    assert calls and calls[-1] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_a_refusal_fires_the_persist_hook():
    calls = []
    bridge = _bridge(persist_fn=lambda b: calls.append(len(b.envelope.refused)))
    # No `to` → a deploy-from-page refusal.
    await _ask(bridge, "eth_sendTransaction", [{"data": "0x00"}])
    assert calls and calls[-1] >= 1


@pytest.mark.asyncio
async def test_a_broken_persist_hook_never_breaks_a_spend():
    def _boom(_b):
        raise RuntimeError("store down")

    bridge = _bridge(persist_fn=_boom)
    out = await _ask(bridge, "eth_sendTransaction",
                     [{"to": POOL, "data": "0x12345678", "from": HOLDER}])
    assert out.get("result")           # the spend still succeeded


# --- envelope_snapshot round-trips through the store ------------------------- #

def test_envelope_snapshot_is_json_able_and_carries_the_address():
    env = B.Envelope(chain="base", max_spend_usd=5.0, session_budget_usd=20.0,
                     spent_usd=3.0)
    snap = B.envelope_snapshot(env, HOLDER)
    assert snap["address"] == HOLDER and snap["chain"] == "base"
    assert snap["spent_usd"] == 3.0
    json.dumps(snap)                    # no raise


# --- the tool: persist on connect-shape, read after a restart --------------- #

def _tool(store):
    tool = DappBrowserTool(wallet=_Wallet())
    tool._store = store
    return tool


def test_persist_bridge_writes_the_live_envelope(tmp_path):
    store = DappSessionStore(str(tmp_path / "dapp_sessions.db"))
    tool = _tool(store)
    bridge = _bridge(ctx=_ctx("sess-X", "u1"))
    bridge.envelope.spent_usd = 4.0
    tool._persist_bridge(bridge)
    row = store.get("sess-X", user_id="u1")
    assert row is not None and row.envelope["spent_usd"] == 4.0
    assert row.envelope["address"] == HOLDER


@pytest.mark.asyncio
async def test_dapp_status_reports_a_persisted_session_after_a_restart(tmp_path):
    path = str(tmp_path / "dapp_sessions.db")
    # Process 1: arm + spend, then persist (what the live tool does).
    tool_a = _tool(DappSessionStore(path))
    bridge = _bridge(ctx=_ctx("sess-R", "u1"))
    bridge.envelope.spent_usd = 6.0
    bridge.envelope.sent.append({"tx": "0xabc", "to": POOL, "selector": "0x1234",
                                 "usd": 6.0})
    tool_a._persist_bridge(bridge)

    # Process 2 (a restart): a FRESH tool with an EMPTY _bridges dict, same file.
    tool_b = _tool(DappSessionStore(path))
    result = await tool_b.dapp_status(_no_params(), execution_context=_ctx("sess-R", "u1"))
    text = result.extracted_content
    assert "saved record" in text            # honest: not a live wallet
    assert "$6.0000" in text
    assert HOLDER in text


@pytest.mark.asyncio
async def test_dapp_status_says_nothing_when_no_row_exists(tmp_path):
    tool = _tool(DappSessionStore(str(tmp_path / "dapp_sessions.db")))
    result = await tool.dapp_status(_no_params(), execution_context=_ctx("gone", "u1"))
    assert "no dapp wallet is connected" in result.extracted_content


def _no_params():
    from tools.dapp_browser.tool import NoParams
    return NoParams()
