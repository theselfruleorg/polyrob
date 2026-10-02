"""071 W4 — wallet intelligence: any wallet's activity, a token's origin, and
Solana launch forensics. Every payload here is a trimmed copy of a real answer
(Solana jsonParsed getTransaction; Blockscout v2), recorded 2026-10-02.
"""
import pytest

from core.wallet import activity as act
from core.wallet import token_origin as orig
from tools.defi.data_tool import DefiDataTool
from tools.defi.wallet_intel import (TokenOriginParams, WalletActivityParams,
                                     fmt_amount, render_origin)

OWNER = "77777nPhGvFUVAj6uq8MGyLneBt4SMwCScYZDzzztdsa"
OTHER = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"
MINT = "EuCrDg3rR81yemr5XsincqWKkHRUBGtRe84i5t9ipump"
CURVE = "CurvePDA1111111111111111111111111111111111"
EVM = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
TOKEN = "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed"


def _tb(owner, mint, amount, dec=6, idx=1):
    return {"accountIndex": idx, "mint": mint, "owner": owner,
            "uiTokenAmount": {"amount": str(amount), "decimals": dec}}


def _stx(keys, pre, post, fee=5000, pre_tok=(), post_tok=(), err=None, signers=1,
         programs=(), block_time=1790876234):
    return {
        "blockTime": block_time, "slot": 1,
        "meta": {"fee": fee, "err": err, "preBalances": list(pre), "postBalances": list(post),
                 "preTokenBalances": list(pre_tok), "postTokenBalances": list(post_tok),
                 "innerInstructions": []},
        "transaction": {"message": {
            "accountKeys": [{"pubkey": k, "signer": i < signers, "source": "transaction"}
                            for i, k in enumerate(keys)],
            "instructions": [{"programId": p} for p in programs]}},
    }


# -- Solana rows ------------------------------------------------------------------

def test_solana_swap_is_two_sided_and_fee_is_separate():
    tx = _stx([OWNER, OTHER], [10_000_000_000, 0], [9_999_895_000 - 5000, 0], fee=5000,
              pre_tok=[_tb(OWNER, USDC, 945_508_531), _tb(OWNER, WSOL, 0, 9)],
              post_tok=[_tb(OWNER, USDC, 0), _tb(OWNER, WSOL, 7_990_624_576, 9)])
    row = act.parse_solana_tx(tx, OWNER, sig="S1")
    assert row.kind == "swap"
    by = {d.asset: d.raw for d in row.deltas}
    assert by[USDC] == -945_508_531 and by[WSOL] == 7_990_624_576
    # the fee is NOT inside the SOL delta (it is added back), and is reported apart
    assert by[act.NATIVE] == -105_000 and row.fee_raw == 5000


def test_solana_receive_names_the_counterparty():
    tx = _stx([OTHER, OWNER], [5_000_000_000, 1_000], [3_999_995_000, 1_000_001_000], fee=5000)
    row = act.parse_solana_tx(tx, OWNER, sig="S2")
    assert row.kind == "receive" and row.counterparty == OTHER
    assert row.fee_raw == 0          # someone else paid it


def test_rent_beside_a_token_receive_is_not_a_swap_side():
    tx = _stx([OWNER, OTHER], [10_000_000, 0], [10_000_000 - 2_039_280 - 5000, 0],
              post_tok=[_tb(OWNER, USDC, 50_000_000), ])
    assert act.parse_solana_tx(tx, OWNER, sig="S3").kind == "receive"


def test_failed_solana_tx_moves_nothing_but_the_fee():
    tx = _stx([OWNER], [10_000], [5_000], err={"InstructionError": [0, "Custom"]})
    row = act.parse_solana_tx(tx, OWNER, sig="S4")
    assert row.kind == "failed" and row.deltas == [] and row.fee_raw == 5000


def test_base58_is_compared_exactly():
    tx = _stx([OTHER, OWNER.lower()], [5_000_000_000, 0], [3_999_995_000, 1_000_000_000])
    row = act.parse_solana_tx(tx, OWNER, sig="S5")
    assert row.deltas == []          # a lowercased base58 key is a DIFFERENT account


def test_solana_activity_counts_what_it_did_not_read():
    sigs = [{"signature": f"sig{i}", "blockTime": 100 - i} for i in range(3)]
    good = _stx([OTHER, OWNER], [2_000_000_000, 0], [999_995_000, 1_000_000_000])

    def rpc(method, params):
        if method == "getSignaturesForAddress":
            assert params[1]["limit"] == 3
            return sigs
        if params[0] == "sig1":
            raise RuntimeError("boom")
        assert params[1]["maxSupportedTransactionVersion"] == act.MAX_TX_VERSION
        return good

    rep = act.solana_activity(OWNER, 3, rpc=rpc)
    assert rep.available and [r.ref for r in rep.rows] == ["sig0", "sig2"]
    assert rep.partial and "1 of 3 transactions" in rep.not_read[0]


def test_solana_activity_unreadable_is_not_empty():
    def rpc(method, params):
        raise RuntimeError("connection refused")
    rep = act.solana_activity(OWNER, 5, rpc=rpc)
    assert rep.available is False and "getSignaturesForAddress failed" in rep.reason


def test_only_a_rate_limit_is_retried():
    calls, naps = [], []

    def flaky(method, params):
        calls.append(method)
        if len(calls) < 3:
            raise RuntimeError("HTTP Error 429: Too Many Requests")
        return "ok"
    assert act.call_with_backoff(flaky, "m", [], sleep=naps.append) == "ok"
    assert naps == [0.5, 1.0]

    def broken(method, params):
        raise ValueError("bad")
    with pytest.raises(ValueError):
        act.call_with_backoff(broken, "m", [], sleep=naps.append)
    assert naps == [0.5, 1.0]


def test_limit_is_capped():
    seen = {}

    def rpc(method, params):
        seen["limit"] = params[1]["limit"]
        return []
    act.solana_activity(OWNER, 500, rpc=rpc)
    assert seen["limit"] == act.MAX_TX


# -- EVM rows (Blockscout) ----------------------------------------------------------

def _addr(h, scam=False):
    return {"hash": h, "is_contract": False, "is_scam": scam}


def _bs(evm_lists, more=False):
    def fetch(url):
        for key, body in evm_lists.items():
            if url.split("?")[0].endswith(key):
                return body
        raise AssertionError(url)
    return fetch


def test_evm_activity_groups_a_swap_across_lists():
    pool = "0xE225a835578c3006784Cb32BEFF71723C6af081c"
    lists = {
        "/transactions": {"items": [
            {"hash": "0xaa", "timestamp": "2026-09-30T14:29:03.000000Z", "from": _addr(EVM),
             "to": _addr(pool), "value": "0", "status": "ok", "fee": {"value": "1000"}},
            {"hash": "0xbb", "timestamp": "2026-09-29T18:17:57.000000Z",
             "from": _addr("0x69bAdF095B03d62e97445EB0142e6488d19fF38B"), "to": _addr(EVM),
             "value": "100000000000", "status": "ok", "fee": {"value": "7"}},
        ], "next_page_params": None},
        "/token-transfers": {"items": [
            {"transaction_hash": "0xaa", "timestamp": "2026-09-30T14:29:03.000000Z",
             "from": _addr(EVM), "to": _addr(pool), "token_type": "ERC-20",
             "token": {"address_hash": TOKEN, "symbol": "DEGEN", "decimals": "18",
                       "type": "ERC-20", "reputation": "ok"},
             "total": {"decimals": "18", "value": "5000000000000000000"}},
            {"transaction_hash": "0xcc", "timestamp": "2026-09-28T00:00:00.000000Z",
             "from": _addr("0x0000000000000000000000000000000000000001", scam=True),
             "to": _addr(EVM), "token_type": "ERC-20",
             "token": {"address_hash": "0x781b41406aF13F9063C9338518190699222A1C5b",
                       "symbol": "Claim at evil.io", "decimals": "8", "reputation": "scam"},
             "total": {"decimals": "8", "value": "268186000000"}},
        ], "next_page_params": None},
        "/internal-transactions": {"items": [
            {"transaction_hash": "0xaa", "timestamp": "2026-09-30T14:29:03.000000Z",
             "from": _addr(pool), "to": _addr(EVM), "value": "2000000000000000", "success": True},
        ], "next_page_params": None},
    }
    rep = act.evm_activity(EVM, "base", 10, fetch=_bs(lists))
    assert rep.available and not rep.partial
    rows = {r.ref: r for r in rep.rows}
    assert [r.ref for r in rep.rows] == ["0xaa", "0xbb", "0xcc"]          # newest first
    assert rows["0xaa"].kind == "swap" and rows["0xaa"].fee_raw == 1000
    assert {d.asset: d.raw for d in rows["0xaa"].deltas} == {
        act.NATIVE: 2_000_000_000_000_000, TOKEN: -5_000_000_000_000_000_000}
    assert rows["0xbb"].kind == "receive" and rows["0xbb"].fee_raw == 0
    assert rows["0xcc"].flags == ["explorer marks scam"]
    flows = {f["asset"]: f for f in act.net_flows(rep.rows)}
    assert flows[act.NATIVE]["net_raw"] == 2_000_000_000_000_000 + 100_000_000_000
    assert flows[TOKEN]["net"] == "-5"


def test_evm_lists_with_different_windows_drop_and_say_so():
    lists = {
        "/transactions": {"items": [
            {"hash": "0x1", "timestamp": "2026-09-30T00:00:00Z", "from": _addr(EVM),
             "to": _addr(TOKEN), "value": "0", "status": "ok", "fee": {"value": "1"}},
            {"hash": "0x2", "timestamp": "2026-01-01T00:00:00Z", "from": _addr(EVM),
             "to": _addr(TOKEN), "value": "0", "status": "ok", "fee": {"value": "1"}},
        ], "next_page_params": None},
        "/token-transfers": {"items": [
            {"transaction_hash": "0x1", "timestamp": "2026-09-30T00:00:00Z",
             "from": _addr(EVM), "to": _addr(TOKEN), "token_type": "ERC-20",
             "token": {"address_hash": TOKEN, "symbol": "D", "decimals": "18"},
             "total": {"decimals": "18", "value": "1"}},
        ], "next_page_params": {"index": 1}},
        "/internal-transactions": {"items": [], "next_page_params": None},
    }
    rep = act.evm_activity(EVM, "base", 10, fetch=_bs(lists))
    assert [r.ref for r in rep.rows] == ["0x1"]
    assert rep.partial and "different windows" in rep.not_read[0] and rep.more_exist


def test_a_chain_without_a_keyless_explorer_says_not_available():
    rep = act.activity(EVM, "robinhood", 10)
    assert rep.available is False and "NOT AVAILABLE on robinhood" in rep.reason


def test_every_pinned_blockscout_host_is_https_v2():
    from core.wallet import chains
    pinned = {r.name: r.blockscout_api for r in chains.all_rows() if r.blockscout_api}
    assert {"ethereum", "base", "arbitrum", "polygon", "optimism"} <= set(pinned)
    assert all(v.startswith("https://") and v.endswith("/api/v2") for v in pinned.values())
    assert chains.get("solana").blockscout_api is None


# -- origin: EVM ----------------------------------------------------------------------

def test_evm_origin_through_a_factory_names_both_and_reads_the_deployer():
    factory = "0xFac70ry000000000000000000000000000000001"
    eoa = "0x3C12B77aE8B7DD1FEB63D1D6a2A819AcdA0a41d2"
    lists = {
        f"addresses/{TOKEN}": {"creator_address_hash": factory, "creation_transaction_hash": "0xc0",
                               "is_contract": True, "is_verified": False, "is_scam": False,
                               "token": {"symbol": "DEGEN", "name": "Degen", "decimals": "18"}},
        "transactions/0xc0": {"from": {"hash": eoa}, "timestamp": "2024-01-07T15:25:35Z"},
        f"addresses/{eoa}/transactions": {"items": [
            {"hash": "0xc1", "timestamp": "2024-01-07T15:25:49Z",
             "created_contract": {"hash": "0x9F07F8A82cB1af1466252e505b7b7ddee103bC91",
                                  "name": "DegenAirdrop1"}}], "next_page_params": {"x": 1}},
        f"addresses/{eoa}/token-transfers": {"items": [
            {"from": {"hash": eoa}, "to": {"hash": TOKEN}, "total": {"value": "3"}},
            {"from": {"hash": TOKEN}, "to": {"hash": eoa}, "total": {"value": "10"}}],
            "next_page_params": None},
    }

    def fetch(url):
        path = url.split("/api/v2/")[1].split("?")[0]
        if path.endswith("/transactions"):
            assert "order=asc" in url
        if path.endswith("/token-transfers"):
            assert f"token={TOKEN}" in url
        return lists[path]

    rep = orig.evm_origin(TOKEN, "base", fetch=fetch, balance_fn=lambda h, c, t: {t[0]: 7})
    assert rep.creator_contract == factory and rep.deployer == eoa
    assert [c.address for c in rep.other_creations] == ["0x9F07F8A82cB1af1466252e505b7b7ddee103bC91"]
    assert rep.other_creations_more is True
    assert (rep.deployer_balance_raw, rep.deployer_sent_raw, rep.deployer_received_raw) == (7, 3, 10)
    text = render_origin(rep)
    assert "a factory or launchpad" in text and '"DegenAirdrop1"' in text
    assert "not verified" in text and "more exist and were NOT read" in text
    assert "a send is not necessarily a sale" in text


def test_evm_origin_unknown_balance_is_said():
    lists = {f"addresses/{TOKEN}": {"creator_address_hash": EVM, "token": {}}}

    def fetch(url):
        path = url.split("/api/v2/")[1].split("?")[0]
        if path in lists:
            return lists[path]
        raise RuntimeError("down")
    rep = orig.evm_origin(TOKEN, "base", fetch=fetch, balance_fn=lambda *a: {TOKEN: None})
    assert rep.deployer == EVM and rep.deployer_balance_raw is None and rep.partial
    assert "deployer holds now: UNKNOWN" in render_origin(rep)


# -- origin: Solana + launch forensics ------------------------------------------------

DEPLOYER = "EzFAwwcd58eSTc57JEsSph6uUH1oaRzhzaUGDvPwdftX"
B1 = "7oYtDd6MVwKvabvAkwdPQmUhCN7zuCri4r87CcqXkX2u"
B2 = "5w8qdi4fmEDxP4yH7S2PwwUww9cauYQssArWBJYhMip7"
FUNDER = "54i4V3AtFrhHYkn4GjkU4HzCEYKpcVv4gyjWhe1KW4Ry"


def _launch_rpc(*, full_pages=0):
    create = _stx([DEPLOYER, CURVE, MINT], [10**10, 0, 0], [9 * 10**9, 0, 0],
                  post_tok=[_tb(CURVE, MINT, 900), _tb(DEPLOYER, MINT, 100)],
                  programs=(orig.PUMP_FUN_PROGRAM,))
    buy1 = _stx([B1, CURVE], [10**9, 0], [5 * 10**8, 0],
                pre_tok=[_tb(CURVE, MINT, 900)], post_tok=[_tb(CURVE, MINT, 850), _tb(B1, MINT, 50)])
    buy2 = _stx([B2, CURVE], [10**9, 0], [5 * 10**8, 0],
                pre_tok=[_tb(CURVE, MINT, 850)], post_tok=[_tb(CURVE, MINT, 820), _tb(B2, MINT, 30)])
    fund = _stx([FUNDER, B1, B2], [10**10, 0, 0], [8 * 10**9 - 5000, 10**9, 10**9])
    mint_sigs = [{"signature": "buy2", "slot": 102, "err": None},
                 {"signature": "late", "slot": 900, "err": None},
                 {"signature": "buy1", "slot": 101, "err": None},
                 {"signature": "create", "slot": 100, "err": None, "blockTime": 1790868354}]
    # newest-first, as the RPC returns them
    mint_sigs = sorted(mint_sigs, key=lambda e: -e["slot"])
    txs = {"create": create, "buy1": buy1, "buy2": buy2, "fund": fund}
    calls = {"pages": 0}

    def rpc(method, params):
        if method == "getSignaturesForAddress":
            addr, opts = params
            if addr == MINT:
                calls["pages"] += 1
                if calls["pages"] <= full_pages:
                    return [{"signature": f"p{calls['pages']}x{i}", "slot": 10**6}
                            for i in range(orig.SIG_PAGE)]
                return mint_sigs
            assert opts["before"] in ("buy1", "buy2", "create")
            return [{"signature": "fund", "err": None}]
        if method == "getTransaction":
            return txs[params[0]]
        if method == "getTokenAccountsByOwner":
            assert params[1] == {"mint": MINT}
            return {"value": []}
        raise AssertionError(method)
    return rpc


def test_solana_origin_finds_deployer_launchpad_and_a_shared_funder():
    rep = orig.solana_origin(MINT, rpc=_launch_rpc())
    assert rep.deployer == DEPLOYER and rep.launchpad == "pump.fun"
    assert rep.creation_ref == "create" and rep.creation_slot == 100
    assert rep.deployer_balance_raw == 0          # the deployer holds none now
    fx = rep.forensics
    assert {b.address for b in fx.buyers} == {DEPLOYER, B1, B2}   # the curve is no signer
    assert list(fx.shared_funders) == [FUNDER] and set(fx.shared_funders[FUNDER]) == {B1, B2}
    text = render_origin(rep)
    assert "SHARED FUNDER" in text and "addresses, not people" in text
    assert "signal, not proof" in text and MINT in text


def test_solana_origin_beyond_the_page_bound_is_not_found_not_absent():
    rep = orig.solana_origin(MINT, rpc=_launch_rpc(full_pages=orig.MAX_SIG_PAGES))
    assert rep.available and rep.deployer is None and rep.forensics is None
    assert "NOT FOUND within the newest" in rep.not_read[0]
    assert "launch forensics: NOT RUN" in render_origin(rep)


# -- the verbs ------------------------------------------------------------------------------

def test_fmt_amount_is_exact():
    assert fmt_amount(1_234_567_890_123, 9) == "1,234.567890123"
    assert fmt_amount(-5 * 10**18, 18) == "-5"
    assert fmt_amount(42, None) == "42 raw units (decimals unknown)"


@pytest.mark.asyncio
async def test_wallet_activity_needs_a_chain_for_0x():
    res = await DefiDataTool(holder=None).wallet_activity(WalletActivityParams(address=EVM))
    assert res.error and "chain=" in res.error


@pytest.mark.asyncio
async def test_wallet_activity_renders_rows_flows_and_metadata(monkeypatch):
    tx = _stx([OTHER, OWNER], [5_000_000_000, 0], [3_999_995_000, 1_000_000_000])
    sigs = [{"signature": "sigA", "blockTime": 1790876234}]

    def rpc(method, params):
        return sigs if method == "getSignaturesForAddress" else tx
    monkeypatch.setattr(act, "_rpc", rpc)
    tool = DefiDataTool(holder=None, kind_fn=lambda c, a: {"kind": "wallet"})
    res = await tool.wallet_activity(WalletActivityParams(address=OWNER))
    assert not res.error
    text = res.extracted_content
    assert "receive" in text and "+1 SOL" in text and OTHER in text and "sigA" in text
    assert "NOT a cost-basis PnL" in text
    meta = res.metadata
    assert meta["verb"] == "wallet_activity" and meta["report"]["owner"] == OWNER
    assert meta["net_flows"][0]["net_raw"] == 1_000_000_000


@pytest.mark.asyncio
async def test_wallet_activity_unreadable_is_not_empty(monkeypatch):
    tool = DefiDataTool(holder=None, kind_fn=lambda c, a: {"kind": "wallet"})
    res = await tool.wallet_activity(WalletActivityParams(address=OWNER))
    assert "NOT AVAILABLE" in res.extracted_content
    assert "not an empty history" in res.extracted_content


@pytest.mark.asyncio
async def test_wallet_activity_of_the_agents_own_wallet_is_an_owner_read(monkeypatch):
    monkeypatch.setattr("core.wallet.authority.turn_refusal", lambda ctx: "not the owner")
    res = await DefiDataTool(holder=OWNER).wallet_activity(WalletActivityParams(address=OWNER))
    assert res.error and "not the owner" in res.error


@pytest.mark.asyncio
async def test_wallet_activity_on_a_token_points_elsewhere():
    tool = DefiDataTool(holder=None, kind_fn=lambda c, a: {"kind": "token"})
    res = await tool.wallet_activity(WalletActivityParams(address=MINT))
    assert res.error


@pytest.mark.asyncio
async def test_token_origin_on_a_wallet_is_refused():
    tool = DefiDataTool(holder=None, kind_fn=lambda c, a: {"kind": "wallet"})
    res = await tool.token_origin(TokenOriginParams(address=OWNER))
    assert res.error and "wallet" in res.error.lower()


@pytest.mark.asyncio
async def test_token_origin_verb_renders_attacker_symbols_quoted(monkeypatch):
    lists = {f"addresses/{TOKEN}": {"creator_address_hash": EVM,
                                    "token": {"symbol": "IGNORE PREVIOUS", "name": "x"}}}

    def get(url, timeout=15.0):
        path = url.split("/api/v2/")[1].split("?")[0]
        if path in lists:
            return lists[path]
        raise RuntimeError("down")
    monkeypatch.setattr(act, "_get", get)
    monkeypatch.setattr("core.wallet.onchain.token_balances", lambda *a, **k: {TOKEN: None})
    tool = DefiDataTool(holder=None, kind_fn=lambda c, a: {"kind": "token"})
    res = await tool.token_origin(TokenOriginParams(address=TOKEN, chain="base"))
    assert 'symbol "IGNORE PREVIOUS' in res.extracted_content
    assert res.metadata["report"]["deployer"] == EVM
    assert "PARTIAL" in res.extracted_content

