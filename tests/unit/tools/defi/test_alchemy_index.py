"""Alchemy holdings indexer — optional, keyed.

EVM has no keyless way to enumerate what an address holds: DexScreener prices a
token you name and GoPlus screens a token you name, but neither lists holdings.
So `portfolio` is complete only when an indexer key is present, and must say so
rather than presenting a partial scan as complete.
"""
import json
import pathlib

from tools.defi.providers import alchemy_index

FX = pathlib.Path(__file__).parent / "fixtures"


def _fx(name):
    return json.loads((FX / name).read_text())


def test_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("ALCHEMY_API_KEY", raising=False)
    assert alchemy_index.available() is False


def test_available_with_key(monkeypatch):
    monkeypatch.setenv("ALCHEMY_API_KEY", "test-key")
    assert alchemy_index.available() is True


def test_blank_key_is_unavailable(monkeypatch):
    monkeypatch.setenv("ALCHEMY_API_KEY", "   ")
    assert alchemy_index.available() is False


def test_parse_balances_returns_nonzero_holdings():
    out = alchemy_index.parse_balances(_fx("alchemy_balances.json"))
    assert out["0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"] == int("0x989680", 16)
    assert out["0x4200000000000000000000000000000000000006"] == int("0x4064fc4b4d4000", 16)


def test_zero_balances_are_not_holdings():
    out = alchemy_index.parse_balances(_fx("alchemy_balances.json"))
    assert "0x940181a94A35A4569E4529A3CDfB74e38FD98631" not in out


def test_errored_entries_are_dropped_not_zeroed():
    """An entry carrying an error is UNKNOWN, so it must not appear as a
    holding — and must certainly not appear as a zero."""
    out = alchemy_index.parse_balances(_fx("alchemy_balances.json"))
    assert "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb" not in out


def test_addresses_are_checksummed():
    """Keys are the canonical EIP-55 form even though the API returns lowercase.

    Compared against to_checksum_address rather than a case-difference
    heuristic: WETH's address is all digits, so it has no letters to case.
    """
    from eth_utils import to_checksum_address

    out = alchemy_index.parse_balances(_fx("alchemy_balances.json"))
    assert out, "fixture must yield holdings"
    for addr in out:
        assert addr == to_checksum_address(addr.lower())


def test_empty_and_malformed_payloads_are_empty():
    assert alchemy_index.parse_balances(None) == {}
    assert alchemy_index.parse_balances({}) == {}
    assert alchemy_index.parse_balances({"result": {}}) == {}
    assert alchemy_index.parse_balances({"error": {"code": -1}}) == {}
