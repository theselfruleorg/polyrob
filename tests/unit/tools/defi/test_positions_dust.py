"""2026-10-14 review C-7 — `defi_data.positions` flags DUST (a known value under $1).

Exit run 72274b3f spent 1.14M tokens trying to sell $0.005 + $0.00009 and then
messaged the owner about it. The verb marks dust in text and metadata; the
exits skill skips it and never escalates it. An unknown value is never dust.
"""
from core import open_positions
from tools.defi import positions_view

MEME = "0xb200000000000000000000ea8625786a776539fb"
POOLS = "0xb2000000000000000000000ff4a547c891ab1b01"


def _px(price, conf="high"):
    return type("P", (), {"price_usd": price, "confidence": conf})()


def _entry(addr, sym, qty):
    return open_positions.PositionEntry(chain="base", address=addr, symbol=sym, qty=qty,
                                        entry_usd=1.0, entry_ts=1.0)


def test_dust_is_flagged_in_text_and_metadata():
    prices = {MEME: _px(1.0), POOLS: _px(0.00005)}
    text, meta = positions_view.build([_entry(MEME, "MEME", 50), _entry(POOLS, "POOLS", 100)],
                                      [], lambda c, a: prices[a], chain="base")
    assert "DUST: worth under $1.00" in text
    assert "1 DUST position(s) under $1.00: POOLS" in text
    flags = {p["symbol"]: p["dust"] for p in meta["positions"]}
    assert flags == {"MEME": False, "POOLS": True}
    assert meta["dust_usd"] == 1.0


def test_unknown_value_is_never_dust():
    assert positions_view.is_dust(None) is False
    text, meta = positions_view.build([_entry(POOLS, "POOLS", 100)], [],
                                      lambda c, a: _px(None), chain="base")
    assert "DUST" not in text and meta["positions"][0]["dust"] is False


def test_exits_skill_skips_dust_and_uses_a_precheck():
    from pathlib import Path
    body = (Path(__file__).resolve().parents[4] / "data/prompts/skills/exits/SKILL.md").read_text()
    assert "Dust — skip it, never escalate it" in body
    assert "do not message the owner about it" in body
    assert "precheck" in body
