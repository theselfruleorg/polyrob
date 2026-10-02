"""071 W0 — "check this address" works, on any chain, for any wallet.

The trigger: a chat user asked Rob to check a Solana wallet. No verb could
read a third-party address, the refusal pointed at a parameter that did not
exist, and the token verbs' schema said "0x… only". These tests pin the fix.
"""
import pytest

import core.wallet.factory as wf
from core.wallet import solana_onchain as so
from core.wallet.address_kind import wrong_kind_for_token, wrong_kind_for_wallet
from core.wallet.solana_onchain import SplHolding, SplHoldings
from tools.defi.data_tool import (ContractReadParams, DefiDataTool,
                                  TokenRefParams, WalletHoldingsParams)
from tools.defi.providers import alchemy_index

SOL_WALLET = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
MINT = "So11111111111111111111111111111111111111112"
OTHER_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
EVM_WALLET = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"

#: Captured at import: the package conftest blocks `so.token_holdings` so that no
#: tool test reaches the network; these tests drive it with a fake `rpc`.
_token_holdings = so.token_holdings


def _price(usd=None, confidence="unknown"):
    return lambda c, a: type("P", (), {
        "price_usd": usd, "confidence": confidence, "pool_count": 0 if usd is None else 3,
        "liquidity_usd": None if usd is None else 1e6})()


def _unknown_identity(c, a):
    return type("I", (), {"symbol": None, "name": None, "decimals": None,
                          "verified": False, "metadata_changed": False})()


# -- solana_onchain: the display enumeration -------------------------------

def _acct(mint, amount, decimals, exts=None):
    info = {"mint": mint, "tokenAmount": {"amount": str(amount), "decimals": decimals}}
    if exts:
        info["extensions"] = [{"extension": e} for e in exts]
    return {"account": {"data": {"parsed": {"info": info}}}}


def test_token_holdings_keeps_decimals_and_reads_token_2022():
    def rpc(method, params):
        prog = params[1]["programId"]
        if prog == so.SPL_TOKEN_PROGRAM:
            return {"value": [_acct(MINT, 1_500_000_000, 9), _acct(OTHER_MINT, 0, 6)]}
        return {"value": [_acct(OTHER_MINT, 42_000_000, 6, ["transferFeeConfig"])]}
    got = _token_holdings(SOL_WALLET, rpc=rpc)
    assert got.token_2022_read is True
    by = {(h.mint, h.token_2022): h for h in got.rows}
    assert by[(MINT, False)].decimals == 9
    t22 = by[(OTHER_MINT, True)]
    assert t22.raw == 42_000_000 and t22.extensions == ("transferFeeConfig",)
    # the zero-balance classic account is a phantom, not a holding
    assert (OTHER_MINT, False) not in by


def test_two_accounts_for_one_mint_are_one_holding():
    def rpc(method, params):
        if params[1]["programId"] == so.SPL_TOKEN_PROGRAM:
            return {"value": [_acct(MINT, 1_000, 9), _acct(MINT, 500, 9)]}
        return {"value": []}
    got = _token_holdings(SOL_WALLET, rpc=rpc)
    assert [(h.mint, h.raw) for h in got.rows] == [(MINT, 1_500)]


def test_a_failed_token_2022_read_is_partial_not_absent():
    def rpc(method, params):
        if params[1]["programId"] == so.TOKEN_2022_PROGRAM:
            raise RuntimeError("429")
        return {"value": [_acct(MINT, 1, 9)]}
    got = _token_holdings(SOL_WALLET, rpc=rpc)
    assert got.token_2022_read is False and len(got.rows) == 1


def test_a_failed_classic_read_is_unknown():
    def rpc(method, params):
        raise RuntimeError("down")
    assert _token_holdings(SOL_WALLET, rpc=rpc) is None


@pytest.mark.parametrize("value,kind", [
    (None, "wallet"),
    ({"owner": so.SYSTEM_PROGRAM, "data": ["", "base64"]}, "wallet"),
    ({"owner": so.SPL_TOKEN_PROGRAM, "data": {"parsed": {"type": "mint", "info": {}}}}, "mint"),
    ({"owner": so.TOKEN_2022_PROGRAM, "data": {"parsed": {"type": "account",
      "info": {"mint": MINT, "owner": SOL_WALLET}}}}, "token_account"),
    ({"owner": "BPFLoaderUpgradeab1e11111111111111111111111", "executable": True}, "program"),
])
def test_account_kind(value, kind):
    got = so.account_kind(SOL_WALLET, rpc=lambda m, p: {"value": value})
    assert got["kind"] == kind


def test_account_kind_read_failure_is_unknown():
    def rpc(m, p):
        raise RuntimeError("x")
    assert so.account_kind(SOL_WALLET, rpc=rpc) is None


# -- wallet_holdings ---------------------------------------------------------

@pytest.fixture
def no_wallet(monkeypatch):
    monkeypatch.setattr(wf, "get_agent_wallet", lambda: None)


def _sol_tool(holdings, *, kind=None, price=None):
    tool = DefiDataTool(identity_fn=_unknown_identity, price_fn=price or _price(),
                        kind_fn=lambda c, a: kind,
                        solana_holdings_fn=lambda h: holdings)
    tool._solana_native = lambda h: 1.25
    return tool


@pytest.mark.asyncio
async def test_wallet_holdings_reads_a_third_party_solana_wallet(no_wallet):
    held = SplHoldings(rows=[SplHolding(MINT, 1_500_000_000, 9),
                             SplHolding(OTHER_MINT, 42_000_000, 6, True, ("transferHook",))])
    res = await _sol_tool(held).wallet_holdings(WalletHoldingsParams(address=SOL_WALLET))
    text = res.extracted_content or ""
    assert not res.error, res.error
    assert f"holdings for {SOL_WALLET} (chain solana)" in text
    assert "1.500000" in text and "42.000000" in text      # decimals from the chain
    assert "raw units" not in text
    assert "Token-2022: transferHook" in text
    assert "coverage: complete" in text
    assert "1.250000000 SOL" in text
    # third-party wording, not "you"
    assert "holdings you bought" not in text


@pytest.mark.asyncio
async def test_wallet_holdings_says_unavailable_not_empty(no_wallet):
    res = await _sol_tool(None).wallet_holdings(WalletHoldingsParams(address=SOL_WALLET))
    assert "UNAVAILABLE" in (res.extracted_content or "")


@pytest.mark.asyncio
async def test_wallet_holdings_names_the_count_past_the_row_cap(no_wallet):
    rows = [SplHolding(f"Mint{i:040d}"[:44], 10, 0) for i in range(DefiDataTool._UNVALUED_ROWS_SHOWN + 7)]
    tool = _sol_tool(SplHoldings(rows=rows))
    tool._validate = lambda c, a: (a, None)
    res = await tool.wallet_holdings(WalletHoldingsParams(address=SOL_WALLET))
    out = res.extracted_content or ""
    n = DefiDataTool._UNVALUED_ROWS_SHOWN + 7
    # Past the cap the rows are COUNTED (every one stays in the metadata).
    assert f"{n} more held" in out and f"{n} unvalued" in out
    assert len(res.metadata["holdings"]) == n


@pytest.mark.asyncio
async def test_a_0x_address_without_a_chain_asks_for_one(no_wallet):
    res = await DefiDataTool().wallet_holdings(WalletHoldingsParams(address=EVM_WALLET))
    assert res.error and "chain=" in res.error and "base" in res.error


@pytest.mark.asyncio
async def test_wallet_holdings_reads_an_evm_wallet(no_wallet):
    tool = DefiDataTool(identity_fn=_unknown_identity, price_fn=_price(),
                        kind_fn=lambda c, a: {"kind": "wallet"},
                        index_fn=lambda h, chain: {},
                        native_fn=lambda h, c: 0.5)
    res = await tool.wallet_holdings(WalletHoldingsParams(address=EVM_WALLET, chain="base"))
    text = res.extracted_content or ""
    assert not res.error, res.error
    assert f"holdings for {EVM_WALLET} (chain base)" in text
    assert "0.500000 ETH" in text


@pytest.mark.asyncio
async def test_wallet_holdings_on_a_token_points_to_token_info(no_wallet):
    res = await _sol_tool(SplHoldings(), kind={"kind": "token"}).wallet_holdings(
        WalletHoldingsParams(address=MINT))
    assert res.error and "is a TOKEN" in res.error and "token_info" in res.error


@pytest.mark.asyncio
async def test_wallet_holdings_of_the_agents_own_wallet_stays_an_owner_read(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-1")

    class _W:
        address = "0x" + "11" * 20
        solana_address = SOL_WALLET

    monkeypatch.setattr(wf, "get_agent_wallet", lambda: _W())

    class _Ctx:
        user_id = "u_stranger"
        workspace_dir = None

    res = await _sol_tool(SplHoldings()).wallet_holdings(
        WalletHoldingsParams(address=SOL_WALLET), execution_context=_Ctx())
    assert res.error and "owner" in res.error
    assert "wallet_holdings" not in res.error      # no self-referential remedy


# -- the token verbs answer "that is a wallet" ------------------------------

@pytest.mark.asyncio
async def test_token_info_on_a_wallet_points_to_wallet_holdings():
    tool = DefiDataTool(identity_fn=_unknown_identity, price_fn=_price(),
                        kind_fn=lambda c, a: {"kind": "wallet"})
    res = await tool.token_info(TokenRefParams(chain="solana", address=SOL_WALLET))
    assert res.error and "WALLET" in res.error and "wallet_holdings" in res.error


@pytest.mark.asyncio
async def test_price_on_a_wallet_points_to_wallet_holdings():
    tool = DefiDataTool(price_fn=_price(), kind_fn=lambda c, a: {"kind": "wallet"})
    res = await tool.price(TokenRefParams(chain="solana", address=SOL_WALLET))
    assert res.error and "wallet_holdings" in res.error


@pytest.mark.asyncio
async def test_an_unreadable_kind_changes_nothing():
    tool = DefiDataTool(price_fn=_price(), kind_fn=lambda c, a: None)
    res = await tool.price(TokenRefParams(chain="solana", address=MINT))
    assert not res.error and "price unknown" in (res.extracted_content or "")


def test_token_account_refusal_names_mint_and_owner():
    msg = wrong_kind_for_token("solana", "X", {"kind": "token_account",
                                               "mint": MINT, "owner": SOL_WALLET})
    assert MINT in msg and SOL_WALLET in msg
    assert wrong_kind_for_wallet("base", "X", {"kind": "contract"}) is None


@pytest.mark.asyncio
async def test_contract_read_on_solana_says_evm_only():
    res = await DefiDataTool().contract_read(ContractReadParams(
        chain="solana", address=MINT, signature="decimals()"))
    assert res.error and "EVM-only" in res.error


# -- schema text -------------------------------------------------------------

def test_the_token_address_field_names_both_families():
    desc = TokenRefParams.model_fields["address"].description
    assert "solana" in desc and "0x" in desc and "wallet_holdings" in desc


def test_the_operator_refusal_names_a_verb_that_exists():
    from tools.defi import data_tool
    import inspect
    src = inspect.getsource(data_tool._operator_read_refusal)
    assert "Pass an explicit address" not in src
    assert hasattr(DefiDataTool, "wallet_holdings")


# -- alchemy paging ----------------------------------------------------------

def _page(entries, key=None):
    res = {"tokenBalances": [{"contractAddress": a, "tokenBalance": hex(v)} for a, v in entries]}
    if key:
        res["pageKey"] = key
    return {"result": res}


A = "0x" + "aa" * 20
B = "0x" + "bb" * 20


def test_alchemy_follows_page_keys_to_the_end():
    pages = {None: _page([(A, 1)], "k1"), "k1": _page([(B, 2)])}
    got, complete = alchemy_index.fetch_all_balances(
        "0x" + "11" * 20, post=lambda h, c, k, t: pages[k])
    assert complete is True and len(got) == 2


def test_alchemy_page_cap_is_reported_incomplete():
    got, complete = alchemy_index.fetch_all_balances(
        "0x" + "11" * 20, post=lambda h, c, k, t: _page([(A, 1)], "more"))
    assert complete is False and got


def test_alchemy_later_page_failure_is_partial_first_is_unknown():
    pages = {None: _page([(A, 1)], "k1"), "k1": None}
    got, complete = alchemy_index.fetch_all_balances(
        "0x" + "11" * 20, post=lambda h, c, k, t: pages[k])
    assert complete is False and len(got) == 1
    assert alchemy_index.fetch_all_balances("0x" + "11" * 20,
                                            post=lambda h, c, k, t: None) is None


@pytest.mark.asyncio
async def test_token_info_fills_an_unpinned_solana_mint_from_chain_and_jupiter(monkeypatch):
    from core.wallet import spl_facts
    from tools.defi.providers import token_audits
    monkeypatch.setattr(spl_facts, "read_mint",
                        lambda m, **k: type("F", (), {"decimals": 5})())
    monkeypatch.setattr(token_audits, "jupiter_token",
                        lambda m: type("J", (), {"symbol": "BONK"})())
    tool = DefiDataTool(identity_fn=_unknown_identity, price_fn=_price(),
                        kind_fn=lambda c, a: {"kind": "token"}, facts_fn=lambda c, a: [])
    res = await tool.token_info(TokenRefParams(chain="solana", address=MINT))
    text = res.extracted_content or ""
    assert '"BONK"' in text and "decimals: 5" in text
    assert "symbol from Jupiter, self-reported" in text
    assert "verified: no" in text


@pytest.mark.asyncio
async def test_every_venue_address_is_an_owner_read(monkeypatch):
    """071 review: the polymarket/hyperliquid/x402 venue keys are the agent's too."""
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-1")
    venue = "0x" + "77" * 20

    class _W:
        address = "0x" + "11" * 20
        solana_address = "So1anaOperator"

        def address_for_venue(self, v):
            return venue if v == "polymarket" else self.address

    monkeypatch.setattr(wf, "get_agent_wallet", lambda: _W())

    class _Ctx:
        user_id = "u_stranger"
        workspace_dir = None

    tool = DefiDataTool(kind_fn=lambda c, a: {"kind": "wallet"}, index_fn=lambda h, chain: {},
                        native_fn=lambda h, c: 1.0, identity_fn=_unknown_identity, price_fn=_price())
    res = await tool.wallet_holdings(WalletHoldingsParams(address=venue, chain="polygon"),
                                     execution_context=_Ctx())
    assert res.error and "owner" in res.error


# -- compact render (owner UX) ------------------------------------------------

@pytest.mark.asyncio
async def test_every_balance_read_failed_is_an_unknown_total_never_zero(no_wallet):
    junk = ["0x" + c * 40 for c in "12"]
    tool = DefiDataTool(identity_fn=_unknown_identity, price_fn=_price(),
                        kind_fn=lambda c, a: {"kind": "wallet"},
                        index_fn=lambda h, chain: {a: None for a in junk},
                        native_fn=lambda h, c: 0.5)
    res = await tool.wallet_holdings(WalletHoldingsParams(address=EVM_WALLET, chain="base"))
    text = res.extracted_content or ""
    assert "total: UNKNOWN — 2 of 2 balance reads failed" in text
    assert "$0.00" not in text
    # A failed read of an unpinned candidate is COUNTED, not listed by address.
    assert junk[0] not in text and "2 other scanned contract(s)" in text
    assert {r["status"] for r in res.metadata["holdings"]} == {"balance_unknown"}


@pytest.mark.asyncio
async def test_zero_balances_are_hidden_but_kept_in_metadata(no_wallet):
    zero, held = "0x" + "3" * 40, "0x" + "4" * 40
    tool = DefiDataTool(identity_fn=lambda c, a: type("I", (), {
                            "symbol": "MEME", "decimals": 18, "name": None})(),
                        price_fn=_price(), kind_fn=lambda c, a: {"kind": "wallet"},
                        index_fn=lambda h, chain: {zero: 0, held: 10 ** 18},
                        native_fn=lambda h, c: 0.5)
    res = await tool.wallet_holdings(WalletHoldingsParams(address=EVM_WALLET, chain="base"))
    text = res.extracted_content or ""
    assert zero not in text and held in text and "0.000000 MEME" not in text
    assert any(r["status"] == "zero" for r in res.metadata["holdings"])


@pytest.mark.asyncio
async def test_a_big_wallet_lists_top_valued_rows_then_counts(no_wallet):
    rows = [SplHolding(f"V{i:043d}"[:44], 10 ** 6 * (i + 1), 6) for i in range(12)]
    tool = _sol_tool(SplHoldings(rows=rows), price=_price(1.0, "high"))
    tool._validate = lambda c, a: (a, None)
    res = await tool.wallet_holdings(WalletHoldingsParams(address=SOL_WALLET))
    text = res.extracted_content or ""
    assert "+2 more valued holding(s)" in text
    # Largest first: the 12th row (12 units) is listed, the 1st (1 unit) is not.
    assert rows[11].mint in text and rows[0].mint not in text
    assert "$78.00" in text                      # the total still counts every row
