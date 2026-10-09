"""CR-H06 / CR-L02 / CR-L18 on the dapp wallet bridge."""
import types

import pytest

from tests.unit.tools.dapp_browser.test_bridge import (  # noqa: F401
    _Page, _Rail, _Wallet, _Gate, _ask, _bridge, _source)
from core.wallet.tx_guard import Decision
from tools.dapp_browser import bridge as B

NPM = "0xC36442b4a4522E871399CD717aBDD847Ab11FE88"
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
POOL = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"


def _send(to):
    return [{"to": to, "data": "0x12345678", "value": "0x0"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("to", [NPM, PERMIT2])
async def test_page_tx_to_a_pinned_value_holder_is_refused(to):
    captured = []
    bridge, gate = _bridge(captured=captured)
    out = await _ask(bridge, "eth_sendTransaction", _send(to))
    assert "error" in out and "typed verb" in out["error"]["message"]
    assert captured == []


@pytest.mark.asyncio
async def test_page_tx_key_is_stable_not_random():
    bridge, gate = _bridge()
    await _ask(bridge, "eth_sendTransaction", _send(POOL))
    key = gate.recorded[0]["idempotency_key"]
    assert key.startswith("dapp:base:" + POOL.lower())
    assert "uuid" not in key and ":n1:" in key


def _ctx_bridge(turn_kind_probe, captured, frozen_kind=None):
    def _guard(intent, tx, **kw):
        captured.append(kw["execution_context"])
        return Decision(allowed=True, reason="t", lane="autonomous", amount_usd=1.0)
    ctx = types.SimpleNamespace(session_id="s1", user_id="u", role="orchestrator",
                                is_sub_agent=False,
                                metadata={"turn_kind": frozen_kind})
    env = B.Envelope(chain="base", max_spend_usd=50.0, session_budget_usd=100.0,
                     allow_contracts=(), approval_timeout_sec=0.5)
    return B.WalletBridge(
        envelope=env, wallet=_Wallet(_Gate()), execution_context=ctx,
        rail_factory=_Rail, guard_fn=_guard, price_fn=lambda c, a: 1.0,
        rpc_fn=lambda *a: "0x6000", armed_origin="https://app.example",
        turn_kind_probe=turn_kind_probe)


@pytest.mark.asyncio
async def test_the_guard_sees_the_LIVE_turn_kind():
    captured = []
    bridge = _ctx_bridge(lambda: "self_wake", captured)
    await _ask(bridge, "eth_sendTransaction", _send(POOL))
    assert captured[0].metadata["turn_kind"] == "self_wake"
    assert bridge._ctx.metadata["turn_kind"] is None  # arming ctx untouched


@pytest.mark.asyncio
async def test_a_forged_arming_turn_stays_forged():
    captured = []
    bridge = _ctx_bridge(lambda: None, captured, frozen_kind="group")
    await _ask(bridge, "eth_sendTransaction", _send(POOL))
    assert captured[0].metadata["turn_kind"] == "group"


@pytest.mark.asyncio
async def test_an_unreadable_live_turn_refuses():
    def _boom():
        raise RuntimeError("no orchestrator")
    captured = []
    bridge = _ctx_bridge(_boom, captured)
    out = await _ask(bridge, "eth_sendTransaction", _send(POOL))
    assert "error" in out
    assert captured == []


def test_tool_taint_probe_fails_tainted_without_an_orchestrator(monkeypatch):
    from tools.dapp_browser.tool import DappBrowserTool
    import tools.ship_common as sc
    monkeypatch.setattr(sc, "resolve_orchestrator", lambda c, sid, resolver=None: None)
    tool = DappBrowserTool(wallet=_Wallet(_Gate()))
    ctx = types.SimpleNamespace(session_id="s1")
    assert tool._taint_probe_for(ctx)() is True
    with pytest.raises(RuntimeError):
        tool._turn_kind_probe_for(ctx)()


def test_tool_turn_kind_probe_reads_the_orchestrator(monkeypatch):
    from tools.dapp_browser.tool import DappBrowserTool
    import tools.ship_common as sc
    orch = types.SimpleNamespace(_forged_turn_kind="delegation_result")
    monkeypatch.setattr(sc, "resolve_orchestrator", lambda c, sid, resolver=None: orch)
    probe = DappBrowserTool(wallet=_Wallet(_Gate()))._turn_kind_probe_for(
        types.SimpleNamespace(session_id="s1"))
    assert probe() == "delegation_result"


@pytest.mark.asyncio
async def test_no_signing_before_the_tab_is_attached():
    bridge, gate = _bridge()
    bridge.require_attached_page = True
    out = await _ask(bridge, "eth_sendTransaction", _send(POOL))
    assert "error" in out and "not yet bound" in out["error"]["message"]
    bridge.attach_page(_Page())
    out = await bridge.handle(_source(bridge._page), __import__("json").dumps(
        {"method": "eth_sendTransaction", "params": _send(POOL)}))
    assert "result" in __import__("json").loads(out)


# -- a page request is never the owner asking (release validation, 1.2.0) ------
#
# The owner arms a dapp session in the owner's own turn. A later page request reused that
# frozen context, so the real guard read it as ``owner_direct``: the pause and the
# autonomous ceiling were skipped for whatever the page asked to sign.

OWNER = "owner-1"


def _owner_armed_bridge(monkeypatch, captured, *, halted=False, extra_meta=None):
    from core.wallet import tx_guard
    from core.wallet.simulation import Deltas
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda: OWNER)
    monkeypatch.setenv("DEFI_AUTONOMOUS_TURN_TRADING", "true")
    value = 10 ** 18  # 1 ETH at $2,000 — far above the autonomous ceiling

    def _real_guard(intent, tx, **kw):
        kw.pop("price_fn", None)
        d = tx_guard.authorize(
            intent, {**tx, "value": value}, **kw,
            simulate_fn=lambda **_: Deltas(ok=True, native_delta=-value,
                                           token_deltas={}, allowance_deltas={},
                                           gas_used=21_000),
            price_fn=lambda chain, addr: 2000.0, fallback_price_fn=None,
            rpc_is_pinned_fn=lambda chain: True,
            halted_fn=lambda: halted, entry_paused_fn=lambda: False)
        captured.append((kw["execution_context"], d))
        return d

    meta = {"turn_kind": None, **(extra_meta or {})}
    ctx = types.SimpleNamespace(session_id="s-owner", user_id=OWNER,
                                role="orchestrator", is_sub_agent=False,
                                metadata=meta)
    env = B.Envelope(chain="base", max_spend_usd=2500.0, session_budget_usd=5000.0,
                     allow_contracts=(), approval_timeout_sec=0.2)
    from core.wallet.policy import PolicyGate
    gate = PolicyGate(max_per_tx_usd=5000.0, daily_cap_usd=10000.0)
    bridge = B.WalletBridge(
        envelope=env, wallet=_Wallet(gate), execution_context=ctx,
        rail_factory=_Rail, guard_fn=_real_guard, price_fn=lambda c, a: 2000.0,
        rpc_fn=lambda *a: "0x6000", armed_origin="https://app.example",
        turn_kind_probe=lambda: None)
    return bridge, ctx


def test_owner_direct_turn_refuses_a_page_request():
    from core.money.authority import PAGE_REQUEST_KEY, owner_direct_turn
    ctx = types.SimpleNamespace(user_id=OWNER, role="orchestrator",
                                is_sub_agent=False,
                                metadata={PAGE_REQUEST_KEY: True})
    assert owner_direct_turn(ctx, lambda c, t: False) is False


@pytest.mark.asyncio
async def test_a_page_request_in_an_owner_armed_session_is_not_owner_direct(monkeypatch):
    captured = []
    bridge, armed = _owner_armed_bridge(monkeypatch, captured)
    out = await _ask(bridge, "eth_sendTransaction",
                     [{"to": POOL, "data": "0x12345678", "value": hex(10 ** 18)}])
    assert captured, "the guard was never consulted"
    ctx, decision = captured[0]
    assert decision.lane != "owner_direct"
    assert "error" in out                     # above the ceiling, nobody approved
    assert armed.metadata.get("page_request") is None   # arming ctx untouched


@pytest.mark.asyncio
async def test_the_pause_binds_a_page_request_in_an_owner_armed_session(monkeypatch):
    captured = []
    bridge, _ = _owner_armed_bridge(monkeypatch, captured, halted=True)
    out = await _ask(bridge, "eth_sendTransaction",
                     [{"to": POOL, "data": "0x12345678", "value": hex(10 ** 18)}])
    assert "error" in out
    assert all(not d.allowed for _, d in captured)


@pytest.mark.asyncio
async def test_an_inherited_owner_grant_does_not_reach_the_guard(monkeypatch):
    captured = []
    bridge, armed = _owner_armed_bridge(
        monkeypatch, captured, extra_meta={"owner_grant": {"max_spend_usd": 5000.0}})
    await _ask(bridge, "eth_sendTransaction",
               [{"to": POOL, "data": "0x12345678", "value": hex(10 ** 18)}])
    ctx, decision = captured[0]
    assert "owner_grant" not in ctx.metadata
    assert decision.lane not in ("owner_direct", "owner_approved")
