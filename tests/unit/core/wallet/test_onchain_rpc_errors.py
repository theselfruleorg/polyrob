"""_rpc must never turn a provider error into a confirmed zero.

Regression cover for the audit finding (2026-08-07 P1-2): `_rpc` returned
`.get("result")`, so a JSON-RPC `{"error": ...}` body yielded None, and the old
`_hex_int` mapped None -> 0. A rate-limited RPC therefore reported "you hold
$0.00" rather than "unknown", and the same swallow advanced the x402 settlement
scan checkpoint past unread blocks.

`balances()` already documents and tests a (None, None)-on-failure contract
(tests/unit/modules/credits/test_balances.py) — these tests pin the code to it.
"""
import json

import pytest

import core.wallet.onchain as onchain


class _FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _urlopen_returning(payload):
    return lambda *a, **k: _FakeResp(payload)


def test_rpc_raises_on_jsonrpc_error(monkeypatch):
    monkeypatch.setattr(
        onchain.urllib.request, "urlopen",
        _urlopen_returning({"jsonrpc": "2.0", "id": 1,
                            "error": {"code": -32005, "message": "block range too large"}}))
    with pytest.raises(onchain.RpcError):
        onchain._rpc("http://x", "eth_getLogs", [], 1.0)


def test_rpc_raises_on_missing_result(monkeypatch):
    monkeypatch.setattr(onchain.urllib.request, "urlopen",
                        _urlopen_returning({"jsonrpc": "2.0", "id": 1}))
    with pytest.raises(onchain.RpcError):
        onchain._rpc("http://x", "eth_getBalance", [], 1.0)


def test_rpc_returns_result_on_success(monkeypatch):
    monkeypatch.setattr(onchain.urllib.request, "urlopen",
                        _urlopen_returning({"jsonrpc": "2.0", "id": 1, "result": "0x2a"}))
    assert onchain._rpc("http://x", "eth_getBalance", [], 1.0) == "0x2a"


def test_hex_int_unknown_is_none_not_zero():
    assert onchain._hex_int(None) is None
    assert onchain._hex_int("0x") is None
    assert onchain._hex_int("0x10") == 16
    # A genuine on-chain zero is still zero — only UNKNOWN is None.
    assert onchain._hex_int("0x0") == 0


def test_balances_returns_none_none_on_rpc_error(monkeypatch):
    monkeypatch.setattr(
        onchain.urllib.request, "urlopen",
        _urlopen_returning({"error": {"code": -32005, "message": "limit"}}))
    assert onchain.balances("0x" + "11" * 20, "base") == (None, None)


def test_balances_reports_a_real_zero_as_zero(monkeypatch):
    """A successful call returning 0 must render 0.0, not None — the honesty
    rule cuts both ways."""
    monkeypatch.setattr(onchain.urllib.request, "urlopen",
                        _urlopen_returning({"jsonrpc": "2.0", "id": 1, "result": "0x0"}))
    native, usdc = onchain.balances("0x" + "11" * 20, "base")
    assert native == 0.0
    assert usdc == 0.0


def test_rpc_url_override(monkeypatch):
    monkeypatch.setenv("DEFI_EVM_RPC_BASE", "https://my.private.rpc")
    assert onchain.rpc_url_for_chain("base") == "https://my.private.rpc"
    monkeypatch.delenv("DEFI_EVM_RPC_BASE")
    assert onchain.rpc_url_for_chain("base") == "https://mainnet.base.org"


def test_rpc_url_unknown_chain_is_empty(monkeypatch):
    monkeypatch.delenv("DEFI_EVM_RPC_NOPE", raising=False)
    assert onchain.rpc_url_for_chain("nope") == ""


# --- one chain table (2026-08-17) -------------------------------------------
# onchain kept its own rpc/USDC/native table beside the registry. Two tables
# describing the same chains is precisely the drift the registry exists to end.

def test_the_chain_table_is_derived_from_the_registry():
    """EVM rows only: this table feeds eth_call/eth_getBalance, so a non-EVM row
    in it would be an address handed to a JSON-RPC that cannot read it."""
    from core.wallet import chains, onchain
    for row in chains.evm_rows():
        cfg = onchain._CHAIN.get(row.name)
        assert cfg is not None, f"{row.name} missing from the read table"
        rpc, usdc, native = cfg
        assert rpc == row.public_rpc
        assert usdc == row.usdc
        assert native == row.native_symbol


def test_a_non_evm_chain_is_absent_from_the_evm_read_table():
    from core.wallet import chains, onchain
    for row in chains.all_rows():
        if row.family != "evm":
            assert row.name not in onchain._CHAIN, row.name


def test_ethereum_is_readable_now_that_it_is_a_known_chain():
    from core.wallet import onchain
    assert "ethereum" in onchain._CHAIN
    assert onchain._CHAIN["ethereum"][1] == "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"


def test_the_pinned_endpoint_still_wins_over_the_registry_default(monkeypatch):
    from core.wallet import onchain
    monkeypatch.setenv("DEFI_EVM_RPC_ROBINHOOD", "https://pinned.example/rpc")
    assert onchain.rpc_url_for_chain("robinhood") == "https://pinned.example/rpc"


def test_an_unknown_chain_has_no_endpoint(monkeypatch):
    from core.wallet import onchain
    monkeypatch.delenv("DEFI_EVM_RPC_NOSUCHCHAIN", raising=False)
    assert onchain.rpc_url_for_chain("nosuchchain") == ""
