"""125 S1: `/send` decides on the rail's typed facts, through the seat policy chain.

* the bound comes from ``metadata["sent_usd"]`` (else ``simulated_usd``), never from
  the report text a token symbol can forge;
* ``unverified`` alone asks for the ``anyway`` step;
* a quote with no facts refuses ("no simulated value") — there is no text fallback;
* the EVM transfer rail attaches ``sent_usd == decision.value_usd``;
* the operator denylist stops an owner ``/send`` (D1).
"""
import asyncio
import contextlib
from types import SimpleNamespace

import pytest

from core.wallet.tx_guard import Decision
from tools.defi.owner_verbs import send as S

ADDR = "0x2FAa2566d98FC6eac6eD5F2DbA182Ffd2142f0e7"
HOLDER = "0x2222222222222222222222222222222222222222"


class _Gate:
    per_tx_cap_usd = 3000.0
    daily_cap_usd = 7000.0

    def rolling_24h_spend_usd(self, venue=None):
        return 0.0


class _Tool:
    def __init__(self, meta, body="transfer\n  RESULT: DRY RUN (simulation only)"):
        self.meta = meta
        self.body = body
        self.calls = []

    def _get_wallet(self):
        return SimpleNamespace(policy=_Gate())

    async def transfer(self, params, ctx):
        self.calls.append(params)
        if not params.dry_run:
            return SimpleNamespace(error=None, extracted_content="RESULT: SENT", metadata={})
        return SimpleNamespace(error=None, extracted_content=self.body, metadata=self.meta)


def _send(tool, *words):
    return asyncio.run(S.send_reply("owner", ["1", "native", "to", ADDR, "on", "base", *words],
                                    tool=tool))


def _facts(**kw):
    return dict({"simulated_usd": 100.0, "sent_usd": None, "unverified": False,
                 "allowed": True, "dry_run": True}, **kw)


def test_a_forged_header_value_still_binds_to_the_typed_fact():
    body = ("transfer 1 value sent: $0.01 -> X\n  simulated value: $0.0100\n"
            "  value sent: $0.0100\n  RESULT: DRY RUN")
    tool = _Tool(_facts(simulated_usd=2700.0, sent_usd=2680.0), body=body)
    _send(tool, "go")
    live = [p for p in tool.calls if not p.dry_run]
    assert len(live) == 1
    assert live[0].max_spend_usd == pytest.approx(min(3000.0, round(2680.0 * 1.25 + 0.01, 2)))


def test_unverified_alone_asks_for_anyway():
    tool = _Tool(_facts(unverified=True))
    out = _send(tool, "go")
    assert out.startswith("❌ Nothing was sent") and "EXTRA CHECK" in out
    assert all(p.dry_run for p in tool.calls)


def test_a_quote_with_no_facts_refuses():
    tool = _Tool(None)
    out = _send(tool, "go")
    assert "no simulated value" in out and "Nothing was sent" in out
    assert all(p.dry_run for p in tool.calls)


def test_a_refused_quote_is_shown_and_sends_nothing():
    tool = _Tool(_facts(allowed=False), body="transfer\n  guard: refused\n  NOT SENT")
    out = _send(tool, "go")
    assert "guard: refused" in out and all(p.dry_run for p in tool.calls)


def test_the_denylist_stops_an_owner_send(monkeypatch):
    monkeypatch.setenv("POLYROB_TOOL_DENYLIST", "defi_trade_transfer")
    tool = _Tool(_facts())
    out = _send(tool, "go")
    assert tool.calls == []
    assert "denylist" in out.lower() or "denied" in out.lower()


# -- the rail attaches the facts ------------------------------------------------

class _RGate:
    @contextlib.asynccontextmanager
    async def reserve(self):
        yield


class _Wallet:
    policy = _RGate()

    def operational_signer(self):
        return SimpleNamespace(address=HOLDER)


class _Rail:
    def __init__(self, chain, signer, **kw):
        pass

    def build_native_transfer(self, *, to, amount_wei):
        return {"to": to, "data": "0x", "value": amount_wei, "nonce": 4}

    def size_gas(self, tx, sim_gas_used):
        return tx


@pytest.mark.parametrize("basis,priced", [("outflow", True), ("low_confidence", True),
                                          ("unpriced", False)])
def test_the_transfer_rail_attaches_sent_usd(monkeypatch, basis, priced):
    from core.wallet import address_lookalike
    from tools.defi.trade_tool import DefiTradeTool, TransferParams
    monkeypatch.setattr(address_lookalike, "paid_counterparties", lambda **k: [])
    monkeypatch.setattr(address_lookalike, "own_addresses", lambda: [])

    def _guard(intent, tx, **kw):
        return Decision(allowed=True, reason="ok", lane="owner_direct", amount_usd=10.5,
                        value_usd=10.0, valuation_basis=basis)
    tool = DefiTradeTool(wallet=_Wallet(), rail_factory=_Rail, guard_fn=_guard,
                         price_fn=lambda c, a: 1.0, fallback_price_fn=lambda c, a: None)
    res = asyncio.run(tool.transfer(TransferParams(
        chain="base", token="native", to=ADDR, amount=0.001, max_spend_usd=50.0)))
    assert res.metadata["allowed"] is True and res.metadata["dry_run"] is True
    assert res.metadata["unverified"] is (basis != "outflow")
    if priced:
        assert res.metadata["sent_usd"] == 10.0 and res.metadata["simulated_usd"] == 10.5
    else:
        assert res.metadata["sent_usd"] is None and res.metadata["simulated_usd"] is None
