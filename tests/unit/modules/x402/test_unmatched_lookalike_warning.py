"""2026-10-14 01:33Z: dust from 0x1932fd…6d03 — a look-alike of the 0x193311…6d03
bounty winner paid nine minutes earlier — reached the owner as a plain
"Unmatched on-chain payment". The notice must name the address it imitates."""
import pytest

from modules.x402.settlement_scan import SettlementScanMixin

REAL = "0x193311148499fF98F4C7eaFC07FB919c838e6d03"
FAKE = "0x1932fd535a60947ec949fe44ef25760fd8fe6d03"


class _Scan(SettlementScanMixin):
    def __init__(self):
        self.sent = []

    async def _push_owner_notice(self, owner, text, source=""):
        self.sent.append(text)

    def _open_room_offer_hint(self):
        return ""


@pytest.fixture
def scan(monkeypatch):
    import modules.x402.invoicing as inv
    monkeypatch.setattr(inv, "_emit", lambda *a, **k: None)
    return _Scan()


def _transfer(sender):
    return {"tx_hash": "0xabc", "from": sender, "amount_usd": 3e-05, "block": 1}


@pytest.mark.asyncio
async def test_a_lookalike_sender_is_named(scan, monkeypatch):
    from core.wallet import address_lookalike
    monkeypatch.setattr(address_lookalike, "paid_counterparties", lambda **k: [REAL])
    await scan._notify_unmatched(_transfer(FAKE), "0xtreasury")
    text = scan.sent[0]
    assert "address poisoning" in text.lower() and REAL in text


@pytest.mark.asyncio
async def test_an_ordinary_sender_gets_no_warning(scan, monkeypatch):
    from core.wallet import address_lookalike
    monkeypatch.setattr(address_lookalike, "paid_counterparties", lambda **k: [REAL])
    await scan._notify_unmatched(_transfer("0x" + "ab" * 20), "0xtreasury")
    assert "poisoning" not in scan.sent[0].lower()


@pytest.mark.asyncio
async def test_unreadable_history_never_blocks_the_notice(scan, monkeypatch):
    from core.wallet import address_lookalike

    def boom(**k):
        raise ValueError("unreadable")
    monkeypatch.setattr(address_lookalike, "paid_counterparties", boom)
    await scan._notify_unmatched(_transfer(FAKE), "0xtreasury")
    assert scan.sent and "Unmatched on-chain payment" in scan.sent[0]
