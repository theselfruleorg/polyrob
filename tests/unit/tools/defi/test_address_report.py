"""/check is the agent read ``defi_data.address_report`` (071; proposal 125 S12).

The action carries typed metadata (``kind_by_chain``, ``token_on``,
``native_by_chain`` — None is UNKNOWN, never 0 — and ``text``); the owner verb
runs it through the seat policy chain and shows the render of its result.
"""
import asyncio

import pytest

from tools.defi.data_address import AddressReportMixin, AddressReportParams, _evm_chains
from tools.defi.owner_verbs import check as C

SOL = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
EVM = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"


class _AR:
    def __init__(self, content=None, error=None, metadata=None):
        self.extracted_content, self.error, self.metadata = content, error, metadata


class _Tool(AddressReportMixin):
    name = "defi_data"

    def __init__(self):
        self.calls = []

    def _ar(self, *, content=None, error=None, metadata=None):
        return _AR(content=content, error=error, metadata=metadata)

    async def token_info(self, params, ctx=None):
        self.calls.append(("token_info", params.model_dump()))
        return _AR(content="token_info ok")

    async def wallet_holdings(self, params, ctx=None):
        self.calls.append(("wallet_holdings", params.model_dump()))
        return _AR(content="wallet_holdings ok")

    async def token_resolve(self, params, ctx=None):
        self.calls.append(("token_resolve", params.model_dump()))
        return _AR(content="token_resolve ok")


def _kind(monkeypatch, by_chain):
    import core.wallet.address_kind as ak

    def classify(chain, addr):
        got = by_chain.get(chain)
        if isinstance(got, Exception):
            raise got
        return got
    monkeypatch.setattr(ak, "classify", classify)


def _report(tool, address, chain=None):
    return asyncio.run(tool.address_report(AddressReportParams(address=address, chain=chain)))


def _check(tool, *words):
    return asyncio.run(C.check_reply("o", list(words), tool=tool))


# -- the action -------------------------------------------------------------

def test_a_token_on_two_chains_names_exactly_those_two(monkeypatch):
    names = _evm_chains()
    on = names[:2]
    _kind(monkeypatch, {c: {"kind": "token" if c in on else "wallet"} for c in names})
    res = _report(_Tool(), EVM)
    assert res.metadata["token_on"] == on
    assert res.metadata["kind_by_chain"][on[0]] == "token"
    assert res.metadata["text"] == res.extracted_content


def test_a_failed_chain_read_is_unknown_never_zero(monkeypatch):
    names = _evm_chains()
    _kind(monkeypatch, {c: ({"kind": "wallet"} if c != names[0] else RuntimeError("rpc"))
                        for c in names})
    import core.wallet.onchain as oc
    monkeypatch.setattr(oc, "balances",
                        lambda a, c, timeout=4.0: (None, None) if c == "polygon" else (1.5, None))
    res = _report(_Tool(), EVM)
    assert res.metadata["kind_by_chain"][names[0]] is None
    assert res.metadata["native_by_chain"]["polygon"] is None
    assert res.metadata["native_by_chain"]["ethereum"] == 1.5
    assert "UNKNOWN (read failed — not zero)" in res.extracted_content


def test_not_an_address_refuses_and_names_token_resolve():
    res = _report(_Tool(), "TKN")
    assert res.error and "token_resolve" in res.error


def test_unknown_chain_is_named():
    res = _report(_Tool(), EVM, "moonchain")
    assert "moonchain" in res.error and "base" in res.error


# -- the owner verb ---------------------------------------------------------

def test_seat_text_equals_the_render_of_the_tool_result(monkeypatch):
    _kind(monkeypatch, {"solana": {"kind": "wallet"}})
    tool = _Tool()
    out = _check(tool, SOL)
    assert out == C.render_check(_report(_Tool(), SOL)) == "wallet_holdings ok"
    assert tool.calls[0] == ("wallet_holdings", {"address": SOL, "chain": "solana"})


def test_no_owner_no_check():
    assert "Only the owner" in asyncio.run(C.check_reply(None, [SOL]))


def test_bare_shows_usage():
    assert "Usage" in asyncio.run(C.check_reply("o", []))


def test_a_mint_gets_the_token_report(monkeypatch):
    _kind(monkeypatch, {"solana": {"kind": "token"}})
    assert _check(_Tool(), SOL) == "token_info ok"


def test_a_token_account_names_mint_and_owner(monkeypatch):
    _kind(monkeypatch, {"solana": {"kind": "token_account", "mint": "M1", "owner": "W1"}})
    tool = _Tool()
    out = _check(tool, SOL)
    assert "/check M1" in out and "/check W1" in out and not tool.calls


def test_an_unreadable_kind_still_reads_holdings(monkeypatch):
    _kind(monkeypatch, {})
    assert _check(_Tool(), SOL, "solana") == "wallet_holdings ok"


def test_a_bare_0x_token_is_found_on_its_chain(monkeypatch):
    _kind(monkeypatch, {"base": {"kind": "token"}})
    tool = _Tool()
    assert _check(tool, EVM) == "token_info ok"
    assert tool.calls[0][1]["chain"] == "base"


def test_a_ticker_gets_candidates():
    tool = _Tool()
    out = _check(tool, "TKN")
    assert out == "Not an address — searched it as a ticker.\ntoken_resolve ok"
    assert tool.calls[0][1] == {"symbol": "TKN"}


def test_the_denylist_stops_the_owner_read(monkeypatch):
    monkeypatch.setenv("POLYROB_TOOL_DENYLIST", "defi_data_address_report")
    _kind(monkeypatch, {"solana": {"kind": "wallet"}})
    tool = _Tool()
    out = _check(tool, SOL)
    assert not tool.calls and "denylist" in out.lower()


def test_owner_text_drops_operator_env_var_remedies():
    out = C.owner_text(
        "unknown (rate-limited). A keyed RPC pinned in DEFI_SOLANA_RPC makes the "
        "per-address read reliable. Unknown is not wide.\n"
        "re-read for a value (or raise DEFI_PORTFOLIO_PRICE_BUDGET_SEC).\n"
        "(no ALCHEMY_API_KEY, so holdings could not be enumerated; x)")
    assert "DEFI_" not in out and "ALCHEMY_API_KEY" not in out
    assert "Unknown is not wide." in out and "re-read for a value." in out


def test_check_is_a_registered_owner_verb_whose_agent_twin_is_the_read():
    from core import verbs
    row = verbs.verb_for("/check")
    assert row is not None and row.agent == "defi_data_address_report"
    assert verbs.room_refused("/check")
