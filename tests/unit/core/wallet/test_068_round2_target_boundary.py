"""068 round-2 review (Codex N1, B1 residue).

N1: a run with a declared ``target_token`` could still acquire other things
through verbs that never NAME their acquisition — a generic ``call`` with no
``receive_token`` (Codex: a USDC-targeted call spending $0.25 native and
receiving an unrelated NFT was authorized), or a dapp page's transaction.

B1 residue: ``os.path.isfile`` read a store behind a parent directory without
search permission as "absent", which lifted every owner pin.
"""
import json
import os
from types import SimpleNamespace

import pytest

from core.wallet import buy_target, token_pins, tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH = "0x4200000000000000000000000000000000000006"
TARGET = "0xbBa60AB93Fc409b1A34371CBF6c3173795Ed2c7e"
FAKE = "0x357A04366240aa3c9d916Aa0F15c3033686C9007"
NFT = "0x4444444444444444444444444444444444444444"
HOLDER = "0x2222222222222222222222222222222222222222"
CALLEE = "0x3333333333333333333333333333333333333333"
TRANSFER = buy_target._TOPIC_TRANSFER


def _ctx(chain="base", address=USDC):
    return SimpleNamespace(role="orchestrator", is_sub_agent=False, user_id="rob",
                           metadata={buy_target.METADATA_KEY: {"chain": chain,
                                                               "address": address}})


def _word(addr):
    return "0x" + addr[2:].lower().rjust(64, "0")


def _transfer_in(contract, amount=5):
    return {"address": contract.lower(),
            "topics": [TRANSFER, _word(CALLEE), _word(HOLDER)],
            "data": hex(amount)}


def _deltas(**kw):
    base = dict(ok=True, native_delta=-250_000_000_000_000, token_deltas={},
                allowance_deltas={}, gas_used=90_000)
    base.update(kw)
    return Deltas(**base)


# ---- the backstop -----------------------------------------------------------

def test_codex_n1_scenario_nft_inflow_is_refused_under_a_usdc_target():
    d = _deltas(holder_nft_in=((NFT.lower(), "erc721", CALLEE.lower(), 7, 1),))
    why = buy_target.simulated_acquisition_refusal(
        _ctx(address=USDC), chain="base", deltas=d, holder=HOLDER)
    assert why and "erc721" in why


def test_an_undeclared_non_target_token_inflow_is_refused():
    d = _deltas(logs=(_transfer_in(FAKE),))
    why = buy_target.simulated_acquisition_refusal(
        _ctx(address=TARGET), chain="base", deltas=d, holder=HOLDER)
    assert why and FAKE.lower() in why


def test_the_target_and_canonical_inflows_pass():
    d = _deltas(logs=(_transfer_in(TARGET), _transfer_in(WETH)),
                token_deltas={TARGET: 10, USDC: 3})
    assert buy_target.simulated_acquisition_refusal(
        _ctx(address=TARGET), chain="base", deltas=d, holder=HOLDER) is None


def test_no_target_means_no_backstop():
    d = _deltas(holder_nft_in=((NFT.lower(), "erc721", CALLEE.lower(), 7, 1),))
    assert buy_target.simulated_acquisition_refusal(
        SimpleNamespace(metadata={}), chain="base", deltas=d, holder=HOLDER) is None


def test_the_guard_runs_the_backstop_after_simulation(monkeypatch):
    import core.wallet.authority as auth
    monkeypatch.setattr(auth, "turn_refusal", lambda ctx: None)
    intent = tx_guard.TxIntent(chain="base", token=None, to=CALLEE,
                               amount_raw=250_000_000_000_000, max_spend_usd=1.0,
                               idempotency_key="k")
    d = _deltas(holder_nft_in=((NFT.lower(), "erc721", CALLEE.lower(), 7, 1),))
    decision = tx_guard.authorize(
        intent, {"to": CALLEE, "data": "0x1249c58b", "value": 250_000_000_000_000,
                 "chainId": 8453, "nonce": 1, "gas": 120_000, "maxFeePerGas": 10 ** 9},
        holder=HOLDER, gate=PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=1000.0),
        execution_context=_ctx(address=USDC),
        simulate_fn=lambda **_: d, price_fn=lambda c, a: 1000.0,
        rpc_is_pinned_fn=lambda c: True, halted_fn=lambda: False,
        entry_paused_fn=lambda: False, forged_fn=lambda c, t: False)
    assert decision.allowed is False
    assert "target token" in decision.reason


# ---- the call verb must name the target -------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("receive_token,min_raw", [(None, 0), (USDC, 0)])
async def test_call_under_a_target_must_declare_a_receipt(receive_token, min_raw):
    from tools.defi.trade_tool import CallParams, DefiTradeTool
    tool = DefiTradeTool(wallet=None)
    res = await tool.call(CallParams(chain="base", to=CALLEE, calldata="0x1249c58b",
                                     value=0.0001, max_spend_usd=1.0,
                                     receive_token=receive_token,
                                     receive_min_raw=min_raw), _ctx(address=USDC))
    assert res.error and "declares no receipt" in res.error


# ---- bridge: the destination is structurally working capital -----------------

def test_bridge_destination_can_only_be_native_or_canonical():
    from tools.defi.bridge_verb import resolve_dest_currency
    with pytest.raises(ValueError):
        resolve_dest_currency(FAKE, "base")
    got, _ = resolve_dest_currency("usdc", "base")
    assert buy_target._is_canonical("base", got)


# ---- dapp: unclassifiable under a target ------------------------------------

@pytest.mark.asyncio
async def test_a_dapp_transaction_is_refused_under_a_target():
    from tests.unit.tools.dapp_browser.test_bridge import _Rail, _ask, _bridge
    from tools.dapp_browser import bridge as B
    _Rail.last = None
    bridge, gate = _bridge()
    bridge._ctx = _ctx(address=TARGET)
    out = await _ask(bridge, "eth_sendTransaction",
                     [{"to": CALLEE, "data": "0xdeadbeef", "value": "0x1"}])
    assert out["error"]["code"] == B.USER_REJECTED
    assert not gate.recorded
    assert bridge.envelope.refused[-1]["kind"] == "target-bound"


# ---- B1 residue: only a genuinely absent file is "no store" -----------------

def test_a_store_behind_an_unsearchable_parent_is_unreadable(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    parent = tmp_path / "wallet"
    parent.mkdir()
    path = str(parent / "token_pins.db")
    token_pins.pin("robinhood", TARGET, "PNL", db_path=path)
    os.chmod(parent, 0o600)  # readable listing, no search (x) bit
    try:
        with pytest.raises(token_pins.PinStoreUnreadable):
            token_pins.all_pins(db_path=path, strict=True)
        state, _rows, _err = token_pins.pins_status(db_path=path)
        assert state == "unreadable"
    finally:
        os.chmod(parent, 0o700)


def test_a_missing_store_is_still_absent(tmp_path):
    path = str(tmp_path / "nowhere" / "token_pins.db")
    assert token_pins.all_pins(db_path=path, strict=True) == []
    assert token_pins.pins_status(db_path=path)[0] == "absent"


def test_open_positions_behind_an_unsearchable_parent_raises_when_strict(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    from core import open_positions
    parent = tmp_path / "data"
    parent.mkdir()
    path = parent / "open_positions.db"
    path.write_bytes(b"")
    os.chmod(parent, 0o600)
    try:
        with pytest.raises(OSError):
            open_positions.entries_for("rob", db_path=str(path), strict=True)
        assert open_positions.entries_for("rob", db_path=str(path)) == {}
    finally:
        os.chmod(parent, 0o700)
