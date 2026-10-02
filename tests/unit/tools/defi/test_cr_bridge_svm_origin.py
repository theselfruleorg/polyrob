"""The Solana-origin bridge leg: CR-M02, CR-M03, CR-M04, CR-L02, CR-L12
(crypto security analysis 2026-09-23).

Drives `perform_bridge` over fakes for the outside world (Relay's quote, the
blockhash, the simulation, the send, the destination balance). The transaction
bytes are REAL — built by `relay_svm.build_transaction` from the payload — so
the instruction decode runs against what would actually be signed.
"""
import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("solders", reason="needs the `solana` extra")

from solders.keypair import Keypair
from solders.pubkey import Pubkey

from core.wallet import bridge_guard
from core.wallet.solana_simulation import SolanaDeltas
from core.wallet.solana_tx_inspect import RELAY_PROGRAM_IDS, SYSTEM_PROGRAM_ID
from tools.defi import bridge_verb as bv

RELAY = next(iter(RELAY_PROGRAM_IDS))
SENDER = str(Keypair().pubkey())
VAULT = str(Pubkey.new_unique())
EVM = "0xcAda546f6A6ddDE31B71aB21eF63d3EBF09Fa553"
AMOUNT = 0.5
AMOUNT_RAW = 500_000_000
FLOOR = 10 ** 15


@pytest.fixture(autouse=True)
def bound_wallet_owner(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner")


class _Gate:
    has_daily_cap = True

    def __init__(self):
        self.checked, self.recorded, self.seen = [], [], set()

    def reserve(self):
        class _CM:
            async def __aenter__(self_):
                return self

            async def __aexit__(self_, *a):
                return False
        return _CM()

    def check(self, **kw):
        self.checked.append(kw)
        key = kw.get("idempotency_key")
        if key in self.seen:
            return SimpleNamespace(allowed=False, reason="replay blocked")
        return SimpleNamespace(allowed=True, reason="")

    def record(self, **kw):
        self.recorded.append(kw)
        self.seen.add(kw.get("idempotency_key"))


class _Wallet:
    address = EVM
    solana_address = SENDER

    def __init__(self):
        self.policy = _Gate()

    def solana_signer(self):
        return SimpleNamespace(address=SENDER)


def _relay_ix():
    return {"programId": RELAY,
            "keys": [{"pubkey": SENDER, "isSigner": True, "isWritable": True},
                     {"pubkey": VAULT, "isSigner": False, "isWritable": True}],
            "data": "0d9e"}


def _transfer_ix():
    data = (2).to_bytes(4, "little") + AMOUNT_RAW.to_bytes(8, "little")
    return {"programId": SYSTEM_PROGRAM_ID,
            "keys": [{"pubkey": SENDER, "isSigner": True, "isWritable": True},
                     {"pubkey": VAULT, "isSigner": False, "isWritable": True}],
            "data": data.hex()}


def _quote(instructions=None, usd=100.0, request_id="0xreq1"):
    return SimpleNamespace(
        request_id=request_id, origin_chain_id=792703809, dest_chain_id=4663,
        recipient=EVM, currency_out="0x0000000000000000000000000000000000000000",
        amount_in_raw=AMOUNT_RAW, min_out_raw=FLOOR, decimals_out=18,
        symbol_in="SOL", symbol_out="ETH", amount_in_usd=usd,
        amount_out_usd=usd, amount_in_formatted=AMOUNT,
        amount_out_formatted=0.03, min_out_formatted=FLOOR / 1e18,
        impact_pct=0.1, time_estimate_sec=10,
        tx_data={"instructions": instructions or [_relay_ix()]})


class _Tool:
    def __init__(self, quote=None, price=200.0):
        self.wallet = _Wallet()
        self.price = price
        self.sent = []
        self.gate_calls = []

    def _ar(self, *, content=None, error=None):
        return SimpleNamespace(content=content, error=error)

    def _get_wallet(self):
        return self.wallet

    def _price(self, chain, addr):
        return self.price

    def _solana_simulate(self, **kw):
        return SolanaDeltas(ok=True, native_delta=-(AMOUNT_RAW + 5_000),
                            fee_lamports=5_000, native_excess=AMOUNT_RAW)

    def _solana_turn_gate(self, ctx, *, exit_shaped_fn):
        self.gate_calls.append(ctx)
        return (None, False, False)

    blockhash_ok = True

    def _solana_blockhash_valid(self, raw_tx):
        return (self.blockhash_ok, "test")

    def _solana_send(self, raw_tx, signer):
        self.sent.append(raw_tx)
        return f"sig{len(self.sent)}"

    def _solana_confirm(self, sig):
        return True, "confirmed"


@pytest.fixture
def rig(monkeypatch, tmp_path):
    state = SimpleNamespace(quote=_quote())

    class _Provider:
        def quote(self, **kw):
            return state.quote

        def status(self, request_id):
            return ("success", "filled")

    monkeypatch.setenv(bv.FLAG, "true")
    monkeypatch.setenv("DEFI_SOLANA_RPC", "https://pinned.example/solana")
    monkeypatch.setenv("DEFI_AUTONOMOUS_MAX_USD", "100000")
    monkeypatch.setattr("core.config_policy.AutonomyConfig.autonomy_halted",
                        staticmethod(lambda: False))
    monkeypatch.setattr("core.wallet.tx_guard._entry_paused", lambda: False)
    monkeypatch.setattr("core.autonomy_control.allows",
                        lambda kind: SimpleNamespace(allowed=True, reason="ok"))
    monkeypatch.setattr("tools.defi.providers.relay_bridge.RelayBridgeProvider",
                        lambda *a, **k: _Provider())
    monkeypatch.setattr("core.wallet.solana_rail.SolanaRail.recent_blockhash",
                        lambda self: "11111111111111111111111111111111")
    monkeypatch.setattr(bridge_guard, "assert_phase1",
                        lambda **kw: SimpleNamespace(ok=True, reason=""))
    monkeypatch.setattr(bridge_guard, "bridges_db_path",
                        lambda: str(tmp_path / "bridges.db"))
    balances = {"v": 10 ** 18}
    monkeypatch.setattr(bridge_guard, "native_balance_raw",
                        lambda addr, chain: balances["v"])

    def _arrive(**kw):
        balances["v"] += FLOOR * 2
        return bridge_guard.ArrivalOutcome(
            bridge_guard.STATE_ARRIVED, "measured", balance_after=balances["v"],
            measured_delta=FLOOR * 2)
    monkeypatch.setattr(bridge_guard, "await_arrival", _arrive)
    return state


def _run(tool, ctx=None, **over):
    params = dict(from_chain="solana", to_chain="robinhood", amount=AMOUNT,
                  token_in="native", token_out="native", dry_run=False)
    params.update(over)
    return asyncio.run(bv.perform_bridge(
        tool, SimpleNamespace(**params), ctx or SimpleNamespace(user_id="owner")))


def _text(r):
    return (r.error or "") + (r.content or "")


def test_a_clean_relay_order_bridges(rig):
    tool = _Tool()
    r = _run(tool)
    assert "RESULT: bridged" in _text(r), _text(r)
    assert tool.sent


# -- CR-M04 ------------------------------------------------------------------

def test_an_order_that_never_calls_relay_is_not_sent(rig):
    rig.quote = _quote(instructions=[_transfer_ix()])
    tool = _Tool()
    r = _run(tool)
    assert "never invokes the pinned Relay program" in _text(r)
    assert not tool.sent


def test_a_system_transfer_beside_the_relay_call_is_not_sent(rig):
    rig.quote = _quote(instructions=[_relay_ix(), _transfer_ix()])
    tool = _Tool()
    r = _run(tool)
    assert "not a Relay deposit" in _text(r)
    assert not tool.sent


# -- CR-M03 ------------------------------------------------------------------

def test_the_spend_is_valued_at_the_pinned_price_not_relays_figure(rig):
    rig.quote = _quote(usd=0.01)           # Relay under-states the outflow
    tool = _Tool(price=200.0)
    _run(tool)
    booked = tool.wallet.policy.recorded[0]["amount_usd"]
    assert booked == pytest.approx((AMOUNT_RAW + 5_000) / 1e9 * 200.0, abs=0.01)


def test_an_unpriced_outflow_refuses(rig):
    tool = _Tool(price=None)
    r = _run(tool)
    assert "could not be priced" in _text(r)
    assert not tool.sent


# -- CR-M02 ------------------------------------------------------------------

def test_the_solana_origin_consults_the_turn_gate(rig):
    tool = _Tool()
    tool._solana_turn_gate = lambda ctx, exit_shaped_fn: (
        "refused: a forged/autonomous turn cannot move funds", False, False)
    r = _run(tool)
    assert "forged/autonomous turn" in _text(r)
    assert not tool.sent


def test_a_forged_turn_is_refused_by_the_real_gate(rig, monkeypatch):
    from tools.defi.trade_tool import DefiTradeTool
    monkeypatch.setattr(
        "tools.controller.action_registration._is_forged_or_autonomous_turn",
        lambda ctx, tool: True)
    monkeypatch.setattr("core.wallet.tx_guard._autonomous_turn_allowed",
                        lambda *a, **k: False)
    tool = _Tool()
    tool._solana_turn_gate = DefiTradeTool._solana_turn_gate.__get__(tool)
    ctx = SimpleNamespace(user_id="owner", role="orchestrator",
                          is_sub_agent=False, metadata={})
    r = _run(tool, ctx)
    assert "cannot move funds" in _text(r)
    assert not tool.sent


def test_a_tool_without_the_gate_fails_closed(rig):
    tool = _Tool()
    tool._solana_turn_gate = None
    r = _run(tool)
    assert "failing closed" in _text(r)


def test_an_autonomous_origin_needs_a_daily_cap(rig):
    tool = _Tool()
    tool.wallet.policy.has_daily_cap = False
    tool._solana_turn_gate = lambda ctx, exit_shaped_fn: (None, True, False)
    r = _run(tool)
    assert "WALLET_DAILY_CAP_USD" in _text(r)
    assert not tool.sent


# -- CR-L02 ------------------------------------------------------------------

def test_the_replay_key_is_the_intent_not_the_quote():
    ctx = SimpleNamespace(session_id="s1")
    kw = dict(origin_id=1, dest_id=2, dest_currency="0x0", amount_in_raw=5,
              recipient=EVM, execution_context=ctx, now=1_000)
    assert bv._idempotency_key(**kw) == bv._idempotency_key(**kw)
    assert bv._idempotency_key(**kw) != bv._idempotency_key(**{**kw, "amount_in_raw": 6})


def test_a_re_quoted_repeat_of_the_same_bridge_is_a_replay(rig):
    tool = _Tool()
    assert "RESULT: bridged" in _text(_run(tool))
    rig.quote = _quote(request_id="0xreq2")        # a fresh Relay request id
    r = _run(tool)
    assert "replay blocked" in _text(r)
    assert len(tool.sent) == 1


# -- CR-L12 ------------------------------------------------------------------

def test_a_second_bridge_into_the_same_balance_waits(rig, monkeypatch):
    tool = _Tool()
    monkeypatch.setattr(bridge_guard, "await_arrival", lambda **kw: (
        bridge_guard.ArrivalOutcome(bridge_guard.STATE_IN_FLIGHT, "waiting")))
    first = _run(tool)
    assert "IN FLIGHT" in _text(first)
    r = _run(tool, SimpleNamespace(user_id="owner", session_id="another"))
    assert "into the same" in _text(r) and "NOT SENT" in _text(r)
    assert len(tool.sent) == 1


# -- CR-M05 parity ---------------------------------------------------------------

def test_an_invalid_blockhash_is_not_signed_or_sent(rig):
    tool = _Tool()
    tool.blockhash_ok = False
    r = _run(tool)
    assert "blockhash" in _text(r)
    assert "NOT SENT" in _text(r)
    assert not tool.sent


def test_a_tool_without_the_blockhash_check_fails_closed(rig):
    tool = _Tool()
    tool._solana_blockhash_valid = None
    r = _run(tool)
    assert "NOT SENT" in _text(r)
    assert not tool.sent
