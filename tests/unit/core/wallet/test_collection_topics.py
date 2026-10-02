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
    assert collection_reveal.REVEAL_SELECTOR == abi.selector(collection_reveal.REVEAL_SIGNATURE)


def test_modules_import_without_eth_utils(monkeypatch):
    import importlib
    import sys
    monkeypatch.setitem(sys.modules, "eth_utils", None)
    for name in ("core.wallet.collection_mint", "core.wallet.collection_reveal"):
        monkeypatch.delitem(sys.modules, name, raising=False)
        importlib.import_module(name)
