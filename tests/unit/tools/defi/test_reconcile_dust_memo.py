"""The buyback's step-1 reconcile must not re-price airdropped dust every call.

2026-10-08 16:00Z: the treasury held 116 tokens, almost all airdrops. Every
reconcile re-read each one's identity and price; with slow DNS the gate passed
its 60 s action budget twice and the buyback stopped. A dust/unsolicited
verdict for an UNCHANGED raw balance is reused — an unrecorded buy changes the
balance, so it is always re-priced.
"""
import pytest

from tools.defi import data_tool
from tools.defi.data_tool import DefiDataTool, ReconcileParams

FLAT_LEDGER = """# Ledger

## Open positions

| Token | Address | Size |
|-------|---------|------|
| *(none)* | | |

## Closed positions
"""

DUST = "0x" + "d" * 40
DUST2 = "0x" + "e" * 40


class _Counter:
    def __init__(self, price):
        self.calls = []
        self.price = price

    def identity(self, chain, addr):
        self.calls.append(("id", addr))
        return type("I", (), {"symbol": "JUNK", "decimals": 18})()

    def quote(self, chain, addr):
        self.calls.append(("px", addr))
        return type("P", (), {"price_usd": self.price, "confidence": "high"})()


@pytest.fixture(autouse=True)
def _fresh_memo():
    data_tool._DUST_MEMO.clear()
    yield
    data_tool._DUST_MEMO.clear()


def _tool(balances, counter):
    return DefiDataTool(holder="0xHOLDER",
                        index_fn=lambda holder, chain: dict(balances),
                        identity_fn=counter.identity, price_fn=counter.quote)


async def _run(tool, ledger):
    res = await tool.reconcile(ReconcileParams(chain="base", ledger_path=str(ledger)))
    assert res.error is None, res.error
    return res.extracted_content or ""


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    p = tmp_path / "ledger.md"
    p.write_text(FLAT_LEDGER)
    return p


@pytest.mark.asyncio
async def test_unchanged_dust_is_not_repriced(ledger):
    c = _Counter(price=1e-12)  # 1e18 units at 18 decimals × 1e-12 = dust
    bal = {DUST: 10**18, DUST2: 5 * 10**18}
    out1 = await _run(_tool(bal, c), ledger)
    assert "CLEAN" in out1
    first = len(c.calls)
    assert first > 0
    out2 = await _run(_tool(bal, c), ledger)
    assert "CLEAN" in out2
    assert len(c.calls) == first, "dust with an unchanged balance was re-read"
    assert "dust" in out2.lower()


@pytest.mark.asyncio
async def test_a_balance_change_is_repriced_and_can_disagree(ledger):
    c = _Counter(price=1e-12)
    await _run(_tool({DUST: 10**18}, c), ledger)
    # An unrecorded buy: the balance moved and the token is now worth $5.
    c.price = 5e-18
    c.calls.clear()
    out = await _run(_tool({DUST: 10**36}, c), ledger)
    assert ("px", DUST) in c.calls
    assert "DISAGREEMENT" in out


@pytest.mark.asyncio
async def test_a_memo_entry_expires(ledger, monkeypatch):
    c = _Counter(price=1e-12)
    await _run(_tool({DUST: 10**18}, c), ledger)
    c.calls.clear()
    real = data_tool.time.monotonic
    monkeypatch.setattr(data_tool.time, "monotonic",
                        lambda: real() + data_tool._DUST_MEMO_TTL_S + 1)
    await _run(_tool({DUST: 10**18}, c), ledger)
    assert ("px", DUST) in c.calls


@pytest.mark.asyncio
async def test_a_priced_holding_is_never_memoised(ledger):
    c = _Counter(price=5.0)  # $5 per token: unexplained, never dust
    await _run(_tool({DUST: 10**18}, c), ledger)
    c.calls.clear()
    out = await _run(_tool({DUST: 10**18}, c), ledger)
    assert ("px", DUST) in c.calls
    assert "DISAGREEMENT" in out
