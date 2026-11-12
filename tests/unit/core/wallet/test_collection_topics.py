"""The collection event topics are literals (a bare install has no eth_utils and
pack discovery imports these modules); each must equal keccak256(signature)."""
import re
from pathlib import Path

import pytest

from core.wallet import collection_mint, collection_reveal

_LINE = re.compile(r'^(TOPIC_\w+) = "(0x[0-9a-f]{64})"  # (.+)$', re.M)


@pytest.mark.parametrize("mod", [collection_mint, collection_reveal])
def test_topic_literals_are_the_keccak_of_their_signature(mod):
    keccak = pytest.importorskip("eth_utils").keccak
    rows = _LINE.findall(Path(mod.__file__).read_text())
    assert rows
    for name, value, sig in rows:
        assert getattr(mod, name) == value
        assert value == "0x" + keccak(sig.encode()).hex(), name


def test_selector_literals_match_their_signature():
    pytest.importorskip("eth_utils")
    from core.wallet import abi
    assert collection_mint.MINT_SELECTOR == abi.selector(collection_mint.MINT_SIGNATURE)
    assert collection_mint.MINT_WL_SELECTOR == abi.selector(collection_mint.MINT_WL_SIGNATURE)
    assert collection_reveal.REVEAL_SELECTOR == abi.selector(collection_reveal.REVEAL_SIGNATURE)


def test_modules_import_without_eth_utils(monkeypatch):
    import importlib
    import sys
    monkeypatch.setitem(sys.modules, "eth_utils", None)
    for name in ("core.wallet.collection_mint", "core.wallet.collection_reveal"):
        monkeypatch.delitem(sys.modules, name, raising=False)
        importlib.import_module(name)


def test_the_sale_selectors_and_event_topics_are_computed():
    """polyrob-desk PolyrobSale: the two buyer calls and the events whose literals the tests use
    (each recomputed from its signature; `cast sig` / `cast keccak` agree)."""
    keccak = pytest.importorskip("eth_utils").keccak
    assert "0x" + keccak(b"mint(address,uint256,uint256)").hex()[:8] == "0x156e29f6"
    assert ("0x" + keccak(b"mintWL(address,uint256,uint256,uint256,bytes32[])").hex()[:8]
            == "0x1e29517f")
    for sig, topic in (
            ("Bid(uint256,address,uint256,uint256,uint256)",
             "0x76b552d768564e2bf5325e2d83fe7184b2838e6e58fc33173aef30980e0269c8"),
            ("Owed(address,uint256)",
             "0x7393c924ac30f9256e6cebcfb78841b1e7ef11bf8b09d87c2257b32b8063c979"),
            ("WLMinted(address,uint256,uint256)",
             "0xf24f224c22aaba18fca84404df73aff01a1ece89ede441926929fe087cdcb677")):
        assert "0x" + keccak(sig.encode()).hex() == topic, sig
