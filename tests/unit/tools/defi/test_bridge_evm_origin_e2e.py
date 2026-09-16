"""Base -> Robinhood, end to end (039 B2).

This is the leg the owner could not run on 2026-09-12. Leg 1 (Solana -> Base)
arrived and was verified; leg 2 hit a bare `an EVM-origin bridge is not wired
yet`. These tests drive `perform_bridge` over fakes for the four things that
touch the outside world — the Relay quote, the EVM rail, the guard, and the
destination balance read — and assert the whole path, not a substring of source.
"""
import asyncio
from types import SimpleNamespace

import pytest

from core.wallet import bridge_guard
from tools.defi import bridge_verb as bv

HOLDER = "0xcAda546f6A6ddDE31B71aB21eF63d3EBF09Fa553"
DEPOSITORY = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
BASE_ID, ROBINHOOD_ID = 8453, 4663
AMOUNT_ETH = 0.036
AMOUNT_RAW = 36 * 10 ** 15
OUT_RAW = 35_980_000_000_000_000          # 0.03598 ETH
FLOOR_RAW = 35_900_000_000_000_000        # 0.0359 ETH


# -- fakes -----------------------------------------------------------------

@pytest.fixture(autouse=True)
def bound_wallet_owner(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner")


class _Gate:
    def __init__(self):
        self.recorded = []

    def reserve(self):
        gate = self

        class _CM:
            async def __aenter__(self):
                return gate

            async def __aexit__(self, *a):
                return False
        return _CM()

    def record(self, **kw):
        self.recorded.append(kw)


class _Signer:
    address = HOLDER

    def sign_transaction(self, tx):
        return b"\x01\x02"


class _Wallet:
    address = HOLDER
    solana_address = "So1anaAddre55Fake11111111111111111111111111"

    def __init__(self):
        self.policy = _Gate()

    def operational_signer(self):
        return _Signer()


class _Tool:
    def __init__(self):
        self.wallet = _Wallet()

    def _ar(self, *, content=None, error=None):
        return SimpleNamespace(content=content, error=error)

    def _get_wallet(self):
        return self.wallet

    def _price(self, chain, addr):
        return 2600.0

    def _fallback_price(self, chain, addr):
        return None


class _Rail:
    """Stands in for EvmRail. Signs nothing, remembers everything."""
    instances = []

    def __init__(self, chain=None, signer=None, **_kw):
        self.chain = chain
        self.sent = None
        self.receipt_status = "success"
        _Rail.instances.append(self)

    def build_call(self, *, to, data, value):
        return {"to": to, "data": data, "value": value, "gas": 120_000,
                "chainId": BASE_ID, "nonce": 3}

    def size_gas(self, tx, sim_gas_used):
        return {**tx, "gas": int(sim_gas_used * 1.5)}

    def sign_and_send(self, tx):
        self.sent = tx
        return "0xbeef"

    def await_receipt(self, tx_hash, **_kw):
        return SimpleNamespace(
            tx_hash=tx_hash, status=self.receipt_status, block_number=1234,
            gas_used=90_000,
            succeeded=(self.receipt_status == "success"))


def _quote(**over):
    d = dict(
        request_id="0xreq", origin_chain_id=BASE_ID, dest_chain_id=ROBINHOOD_ID,
        sender=HOLDER, recipient=HOLDER,
        currency_in="0x0000000000000000000000000000000000000000",
        currency_out="0x0000000000000000000000000000000000000000",
        amount_in_raw=AMOUNT_RAW, amount_out_raw=OUT_RAW, min_out_raw=FLOOR_RAW,
        decimals_in=18, decimals_out=18, symbol_in="ETH", symbol_out="ETH",
        amount_in_usd=93.6, amount_out_usd=93.55, impact_pct=-0.05,
        time_estimate_sec=12, deposit_address=DEPOSITORY,
        tx_data={"to": DEPOSITORY, "data": "0xdeadbeef",
                 "value": str(AMOUNT_RAW), "chainId": BASE_ID},
        svm_origin=False, raw={},
        amount_in_formatted=AMOUNT_ETH,
        amount_out_formatted=OUT_RAW / 1e18,
        min_out_formatted=FLOOR_RAW / 1e18,
    )
    d.update(over)
    return SimpleNamespace(**d)


class _Provider:
    def __init__(self, quote=None, status=("success", "filled")):
        self._quote = quote or _quote()
        self._status = status

    def quote(self, **_kw):
        return self._quote

    def status(self, request_id):
        return self._status


def _params(**over):
    d = dict(from_chain="base", to_chain="robinhood", amount=AMOUNT_ETH,
             token_in="native", token_out="native", dry_run=False)
    d.update(over)
    return SimpleNamespace(**d)


@pytest.fixture
def rig(monkeypatch, tmp_path):
    """Arms the verb and replaces exactly the four outside-world seams."""
    _Rail.instances.clear()
    monkeypatch.setenv(bv.FLAG, "true")
    monkeypatch.setenv("DEFI_EVM_RPC_BASE", "https://pinned.example/base")
    monkeypatch.setattr("core.wallet.tx_guard._halted", lambda: False)
    monkeypatch.setattr("core.wallet.tx_guard._entry_paused", lambda: False)
    monkeypatch.setattr("core.autonomy_control.allows",
                        lambda kind: SimpleNamespace(allowed=True, reason="ok"))
    monkeypatch.setattr("tools.defi.providers.relay_bridge.RelayBridgeProvider",
                        lambda *a, **k: _Provider())
    monkeypatch.setattr("core.wallet.broadcast.evm.EvmRail", _Rail)
    monkeypatch.setattr(bridge_guard, "bridges_db_path",
                        lambda: str(tmp_path / "bridges.db"))
    # Destination balance: before, then after the arrival.
    seq = iter([10 ** 15, 10 ** 15 + OUT_RAW, 10 ** 15 + OUT_RAW])
    monkeypatch.setattr(bridge_guard, "native_balance_raw",
                        lambda addr, chain: next(seq, 10 ** 15 + OUT_RAW))
    # The guard itself is exercised in tests/unit/core/wallet; here it answers.
    monkeypatch.setattr(
        "core.wallet.tx_guard.authorize",
        lambda intent, tx, **kw: SimpleNamespace(
            allowed=True, reason="authorized", lane="autonomous",
            amount_usd=93.6, sim_gas_used=100_000))
    return SimpleNamespace(tmp=tmp_path)


def _run(tool, params, ctx=None):
    return asyncio.run(bv.perform_bridge(tool, params, ctx or SimpleNamespace(user_id="owner")))


# -- the path that did not exist -------------------------------------------

def test_an_evm_origin_bridge_broadcasts_and_proves_the_arrival(rig):
    tool = _Tool()
    r = _run(tool, _params())
    text = r.content or r.error or ""
    assert "an EVM-origin bridge is not wired yet" not in text
    assert "BROADCAST: 0xbeef" in text
    assert "origin  : confirmed on-chain" in text
    assert "phase 2 : ARRIVED" in text
    assert "RESULT: bridged" in text


def test_the_transaction_carries_the_quoted_value(rig):
    tool = _Tool()
    _run(tool, _params())
    sent = _Rail.instances[0].sent
    assert sent["value"] == AMOUNT_RAW
    assert sent["to"] == DEPOSITORY
    assert sent["gas"] == 150_000, "gas must be sized from the simulation"


def test_the_rail_is_built_for_the_ORIGIN_chain(rig):
    """A rail built for the wrong chain reads the wrong nonce and the wrong fee
    market, and signs them for a chain id that is not theirs."""
    _run(_Tool(), _params())
    assert _Rail.instances[0].chain == "base"


def test_the_spend_is_recorded_to_the_ledger(rig):
    """Every OTHER money verb counts this against ITS cap through the ledger."""
    tool = _Tool()
    _run(tool, _params())
    assert tool.wallet.policy.recorded, "the bridge must reach the ledger"
    rec = tool.wallet.policy.recorded[0]
    assert rec["action"] == "bridge"
    assert rec["amount_usd"] == pytest.approx(93.6)
    assert rec["result_ref"] == "0xbeef"


def test_a_reverted_origin_is_terminal_and_never_in_flight(rig, monkeypatch):
    """The fee was paid and the value did not move. Calling that `in_flight`
    would say 'your money is between two chains' about money that never left."""
    class _Reverted(_Rail):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.receipt_status = "failed"
    monkeypatch.setattr("core.wallet.broadcast.evm.EvmRail", _Reverted)
    r = _run(_Tool(), _params())
    text = r.content or ""
    assert "REVERTED ON-CHAIN" in text
    assert "RESULT: not bridged" in text


def test_no_receipt_is_unproven_and_warns_against_a_resend(rig, monkeypatch):
    class _Pending(_Rail):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.receipt_status = "pending"
    monkeypatch.setattr("core.wallet.broadcast.evm.EvmRail", _Pending)
    r = _run(_Tool(), _params())
    text = r.content or ""
    assert "NOT CONFIRMED" in text
    assert "Do NOT re-send" in text
    assert "0xbeef" in text
    assert "expired blockhash" not in text, "that reason is Solana's, not EVM's"


def test_a_guard_refusal_broadcasts_nothing(rig, monkeypatch):
    monkeypatch.setattr(
        "core.wallet.tx_guard.authorize",
        lambda intent, tx, **kw: SimpleNamespace(
            allowed=False, reason="refused: simulation shows a native INFLOW for a send",
            lane="refuse", amount_usd=None, sim_gas_used=None))
    r = _run(_Tool(), _params())
    assert "RESULT: NOT SENT" in (r.content or "")
    assert all(i.sent is None for i in _Rail.instances)


def test_an_above_ceiling_leg_asks_the_owner_then_proceeds(rig, monkeypatch):
    """`lane="owner_queue"` means ask, not no. The tap must reach the durable
    queue and the bridge must then run — not dead-end."""
    monkeypatch.setattr(
        "core.wallet.tx_guard.authorize",
        lambda intent, tx, **kw: SimpleNamespace(
            allowed=False, reason="owner approval required: $93.60 is above $25.00",
            lane="owner_queue", amount_usd=93.6, sim_gas_used=100_000))
    asked = {}

    class _Approver:
        def __init__(self, **kw):
            pass

        async def request(self, name, summary, ctx, *, hash_params=None):
            asked["name"], asked["summary"] = name, summary
            asked["grant_key"] = hash_params
            return True
    monkeypatch.setattr("tools.controller.approval_queue.OwnerQueueApprover", _Approver)
    r = _run(_Tool(), _params())
    assert asked["name"] == "defi_trade_bridge"
    assert asked["summary"]["recipient"] == HOLDER
    # The grant is keyed on the STABLE intent, never on the re-quoted figures —
    # otherwise every attempt mints a new tap and no approval is ever redeemable.
    assert asked["grant_key"] == {
        "from_chain": "base", "to_chain": "robinhood", "amount": AMOUNT_ETH,
        "token_out": "native", "recipient": HOLDER}
    assert "request_id" not in asked["grant_key"]
    assert "min_out" not in asked["grant_key"]
    assert "RESULT: bridged" in (r.content or "")


def test_a_denied_owner_tap_broadcasts_nothing(rig, monkeypatch):
    monkeypatch.setattr(
        "core.wallet.tx_guard.authorize",
        lambda intent, tx, **kw: SimpleNamespace(
            allowed=False, reason="owner approval required",
            lane="owner_queue", amount_usd=93.6, sim_gas_used=100_000))

    class _Deny:
        def __init__(self, **kw):
            pass

        async def request(self, *a, **k):
            return False
    monkeypatch.setattr("tools.controller.approval_queue.OwnerQueueApprover", _Deny)
    r = _run(_Tool(), _params())
    assert "RESULT: NOT SENT" in (r.content or "")
    assert all(i.sent is None for i in _Rail.instances)


def test_an_order_that_spends_more_than_quoted_is_refused_before_building(rig, monkeypatch):
    bad = _quote(tx_data={"to": DEPOSITORY, "data": "0x",
                          "value": str(AMOUNT_RAW * 100), "chainId": BASE_ID})
    monkeypatch.setattr("tools.defi.providers.relay_bridge.RelayBridgeProvider",
                        lambda *a, **k: _Provider(quote=bad))
    r = _run(_Tool(), _params())
    assert "RESULT: NOT SENT" in (r.content or "")
    assert "not the declared" in (r.content or "")


def test_a_dry_run_reports_the_real_lane_and_sends_nothing(rig):
    r = _run(_Tool(), _params(dry_run=True))
    text = r.content or ""
    assert "[DRY RUN] nothing was broadcast." in text
    assert "always owner-approved" not in text, "that policy ended on 2026-09-12"
    assert "lane:" in text
    assert all(i.sent is None for i in _Rail.instances)


def test_a_quote_with_no_usd_value_refuses_before_the_guard(rig, monkeypatch):
    """Passing an unknown through as max_spend_usd=0.0 would refuse with
    'exceeds the declared max_spend_usd $0.0000' — true, and useless."""
    monkeypatch.setattr("tools.defi.providers.relay_bridge.RelayBridgeProvider",
                        lambda *a, **k: _Provider(quote=_quote(amount_in_usd=None)))
    r = _run(_Tool(), _params())
    text = r.content or r.error or ""
    assert "no USD value" in text
    assert "max_spend_usd" not in text


def test_bridging_INTO_solana_still_refuses(rig):
    r = _run(_Tool(), _params(to_chain="solana"))
    assert "bridging INTO Solana is not supported" in (r.error or "")


def test_the_amount_is_converted_with_Decimal_not_float(rig):
    """`int(round(0.036 * 10**18))` is 35999999999999996.

    Binary floating point cannot hold 0.036, so the amount quoted and signed was
    not the amount the owner typed. Four wei is harmless on its own; the same
    expression is what phase 1 compares the quote against and what the arrival
    floor derives from, and neither can be 'close enough'.
    """
    seen = {}

    class _Recording(_Provider):
        def quote(self, **kw):
            seen.update(kw)
            return self._quote

    import tools.defi.providers.relay_bridge as rb
    orig = rb.RelayBridgeProvider
    rb.RelayBridgeProvider = lambda *a, **k: _Recording()
    try:
        _run(_Tool(), _params(amount=0.036))
    finally:
        rb.RelayBridgeProvider = orig
    assert seen["amount_in_raw"] == 36_000_000_000_000_000


def test_a_refusal_is_stated_exactly_once(rig, monkeypatch):
    """Seen on the first prod dry run: the guard verdict printed twice, once from
    the prepared header and once appended by the caller. Two copies of one
    refusal read like two different problems."""
    monkeypatch.setattr(
        "core.wallet.tx_guard.authorize",
        lambda intent, tx, **kw: SimpleNamespace(
            allowed=False, lane="refuse", amount_usd=91.06, sim_gas_used=None,
            reason=("refused by PolicyGate: daily spend cap $150.00 would be "
                    "exceeded (trailing-24h $94.87 + $91.06)")))
    text = _run(_Tool(), _params(dry_run=True)).content or ""
    assert text.count("refused by PolicyGate") == 1, text
    assert "RESULT: NOT SENT" in text
