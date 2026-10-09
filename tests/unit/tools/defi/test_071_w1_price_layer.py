"""071 W1 in the tools tier: the sources, the batched holdings path, the
renders, the typed metadata, the one-function call sites and the GoPlus cache.

No network: the conftest blocks every provider ``_get``; sources are stubbed.
"""
import asyncio
import pathlib
import re
import types

import pytest

from tools.defi import price_sources as PS
from tools.defi.providers import dexscreener, geckoterminal, jupiter_price
from tools.defi.providers.base import PriceInfo

REPO = pathlib.Path(__file__).resolve().parents[4]
TOK = "0x" + "12" * 20
TOK2 = "0x" + "34" * 20
MINT = "So11111111111111111111111111111111111111112"


def _info(price, liq=2_000_000.0, pools=3, conf="high", pool="0xpool"):
    return PriceInfo(price_usd=price, liquidity_usd=liq, pool_count=pools,
                     confidence=conf, priced_liquidity_usd=liq, priced_pool_address=pool)


def _stub(monkeypatch, *, ds=None, gt=None, jup=None):
    if ds is not None:
        monkeypatch.setattr(dexscreener, "token", lambda chain, addr, **kw: ds)
    if gt is not None:
        monkeypatch.setattr(geckoterminal, "token_prices",
                            lambda chain, addrs, **kw: {a: gt for a in addrs})
    if jup is not None:
        monkeypatch.setattr(jupiter_price, "prices", lambda mints: {m: jup for m in mints})


# -- parsing (pure) ----------------------------------------------------------

def test_ds_batch_keeps_only_base_side_pairs_and_marks_one_pool():
    payload = [
        {"baseToken": {"address": TOK.upper().replace("0X", "0x")}, "priceUsd": "2.5",
         "liquidity": {"usd": 70_000}, "pairAddress": "0xP1"},
        {"baseToken": {"address": "0x" + "99" * 20}, "quoteToken": {"address": TOK2},
         "priceUsd": "9", "liquidity": {"usd": 1e9}, "pairAddress": "0xP2"},
    ]
    got = dexscreener.parse_batch(payload, [TOK, TOK2])
    assert set(got) == {TOK}
    assert got[TOK].price_usd == 2.5 and got[TOK].pool_count == 1
    assert got[TOK].confidence == "low"          # one pair can never grade high


def test_ds_batch_failure_is_none_not_an_empty_map(monkeypatch):
    monkeypatch.setattr(dexscreener, "_get", lambda url, timeout=8.0: None)
    assert dexscreener.tokens_batch("base", [TOK]) is None


def test_ds_token_names_an_outage(monkeypatch):
    monkeypatch.setattr(dexscreener, "_get", lambda url, timeout=8.0: None)
    info = dexscreener.token("base", TOK)
    assert info.price_usd is None and info.error


def test_gt_simple_price_parse_maps_lowercased_evm_keys_back():
    payload = {"data": {"attributes": {"token_prices": {TOK: "1.5", TOK2: "0"}}}}
    got = geckoterminal.parse_token_prices(payload, [TOK.upper().replace("0X", "0x"), TOK2])
    assert list(got.values()) == [1.5]


def test_gt_token_prices_chunks_by_thirty():
    urls = []
    addrs = ["0x" + f"{i:040x}" for i in range(1, 62)]
    geckoterminal.token_prices("base", addrs, fetch=lambda url: urls.append(url) or {})
    assert len(urls) == 3


def test_jupiter_parse_is_case_sensitive_and_drops_zero():
    payload = {MINT: {"usdPrice": 118.9}, "Bad": {"usdPrice": 0}}
    assert jupiter_price.parse_prices(payload, [MINT, "Bad", MINT.lower()]) == {MINT: 118.9}


def test_jupiter_refuses_a_non_base58_id(monkeypatch):
    seen = []
    monkeypatch.setattr(jupiter_price, "_get", lambda url: seen.append(url) or {})
    jupiter_price.prices(["abc&x=1", MINT])
    assert seen and "&x=1" not in seen[0]


# -- the sources behind the one function ------------------------------------

def test_quote_lists_every_source_and_agrees(monkeypatch):
    _stub(monkeypatch, ds=_info(1.0), gt=1.004)
    q = PS.quote("base", TOK)
    assert [n for n, _ in q.sources] == ["dexscreener", "geckoterminal"]
    assert q.confidence == "high"


def test_a_blocked_secondary_is_named_and_does_not_demote(monkeypatch):
    _stub(monkeypatch, ds=_info(1.0))          # geckoterminal._get is blocked
    q = PS.quote("base", TOK)
    assert q.confidence == "high"
    assert [n for n, _ in q.failed] == ["geckoterminal"]


def test_solana_asks_jupiter_too(monkeypatch):
    _stub(monkeypatch, ds=_info(118.9), gt=118.7, jup=118.8)
    q = PS.quote("solana", MINT)
    assert [n for n, _ in q.sources] == ["dexscreener", "geckoterminal", "jupiter"]


def test_disputed_is_never_a_spend_price(monkeypatch):
    _stub(monkeypatch, ds=_info(1.0), gt=1.2)
    assert PS.spend_price("base", TOK) is None
    assert PS.exit_price("base", TOK) == 1.0      # an exit is never blocked by a dispute
    assert PS.indexer_price("base", TOK) is None


def test_the_trade_tool_guard_price_reads_the_layer(monkeypatch):
    from tools.defi.trade_tool import DefiTradeTool
    _stub(monkeypatch, ds=_info(1.0), gt=1.2)
    tool = DefiTradeTool.__new__(DefiTradeTool)
    tool._price_fn = None
    tool._fallback_price_fn = None
    assert tool._price("base", TOK) is None
    assert tool._fallback_price("base", TOK) == 1.0   # exit-exemption price survives a dispute


def test_the_signer_entry_prices_through_the_same_layer(monkeypatch):
    from tools.defi import signer_entry
    _stub(monkeypatch, ds=_info(3.0), gt=3.0)
    assert signer_entry.guard_price("base", TOK) == 3.0
    assert signer_entry.guard_fallback_price("base", TOK) == 3.0


def test_no_call_site_talks_to_dexscreener_token_directly():
    """R5: one read layer. ``dexscreener.token`` is called only by the source
    registration (and the provider module itself)."""
    allowed = {"tools/defi/price_sources.py", "tools/defi/providers/dexscreener.py"}
    pat = re.compile(r"dexscreener\.token\(")
    bad = []
    for top in ("tools", "core", "surfaces", "api", "cli", "cron", "webview"):
        for path in (REPO / top).rglob("*.py"):
            rel = path.relative_to(REPO).as_posix()
            if rel in allowed:
                continue
            for line in path.read_text(errors="replace").splitlines():
                if pat.search(line.split("#", 1)[0]):
                    bad.append(rel)
    assert not bad, bad


# -- prefetch ------------------------------------------------------------------

def test_prefetch_skips_the_per_row_call_for_a_token_with_no_pair(monkeypatch):
    monkeypatch.setattr(dexscreener, "tokens_batch",
                        lambda chain, addrs, **kw: {TOK: _info(1.0, pools=1, conf="low")})
    monkeypatch.setattr(geckoterminal, "token_prices", lambda chain, addrs, **kw: {TOK: 1.0})
    asked = []
    monkeypatch.setattr(dexscreener, "token",
                        lambda chain, addr, **kw: asked.append(addr) or _info(1.0))
    pre = PS.prefetch("base", [TOK, TOK2])
    q_none = PS.quote_prefetched("base", TOK2, pre)
    q_tok = PS.quote_prefetched("base", TOK, pre)
    assert asked == [TOK]
    assert q_none.usd is None and q_none.confidence == "unknown"
    assert q_tok.confidence == "high"


def test_a_token_we_hold_on_purpose_is_always_asked(monkeypatch):
    monkeypatch.setattr(dexscreener, "tokens_batch", lambda chain, addrs, **kw: {})
    monkeypatch.setattr(geckoterminal, "token_prices", lambda chain, addrs, **kw: {})
    asked = []
    monkeypatch.setattr(dexscreener, "token",
                        lambda chain, addr, **kw: asked.append(addr) or _info(1.0))
    pre = PS.prefetch("base", [TOK])
    assert PS.quote_prefetched("base", TOK, pre, must_ask_primary=True).confidence == "high"
    assert asked == [TOK]


def test_the_prefetch_is_capped_and_a_row_past_the_cap_is_priced_as_before(monkeypatch):
    """3,092 mints batched in full spent the whole 40 s budget before row one."""
    monkeypatch.setattr(PS, "PREFETCH_MAX", 1)
    seen = []
    monkeypatch.setattr(dexscreener, "tokens_batch",
                        lambda chain, addrs, **kw: seen.append(list(addrs)) or {})
    monkeypatch.setattr(geckoterminal, "token_prices", lambda chain, addrs, **kw: {})
    asked = []
    monkeypatch.setattr(dexscreener, "token",
                        lambda chain, addr, **kw: asked.append(addr) or _info(1.0))
    pre = PS.prefetch("base", [TOK, TOK2])
    assert seen == [[TOK]]
    assert PS.quote_prefetched("base", TOK2, pre).confidence == "high"
    assert asked == [TOK2]                   # not read as "no pair"


def test_a_failed_secondary_batch_is_named_per_row(monkeypatch):
    monkeypatch.setattr(dexscreener, "tokens_batch", lambda chain, addrs, **kw: None)
    _stub(monkeypatch, ds=_info(1.0))
    pre = PS.prefetch("base", [TOK])                 # geckoterminal._get blocked
    q = PS.quote_prefetched("base", TOK, pre)
    assert [n for n, _ in q.failed] == ["geckoterminal"]


# -- renders + metadata ------------------------------------------------------------

class _Ident:
    def __init__(self, symbol="TOK", decimals=18, verified=False):
        self.symbol, self.name, self.decimals, self.verified = symbol, "Token", decimals, verified
        self.metadata_changed = False
        self.source = None


def _data_tool(**kw):
    from tools.defi.data_tool import DefiDataTool
    return DefiDataTool(identity_fn=lambda chain, addr: _Ident(), **kw)


def _run(coro):
    return asyncio.run(coro)


def test_price_renders_sources_spread_and_metadata(monkeypatch):
    from tools.defi.data_tool import TokenRefParams
    _stub(monkeypatch, ds=_info(1.0), gt=1.004)
    res = _run(_data_tool().price(TokenRefParams(chain="base", address=TOK)))
    assert "sources: dexscreener $1.00 · geckoterminal $1.00" in res.extracted_content
    assert "spread 0.40%" in res.extracted_content
    md = res.metadata["quote"]
    assert md["confidence"] == "high" and len(md["sources"]) == 2


def test_price_renders_disputed_loudly(monkeypatch):
    from tools.defi.data_tool import TokenRefParams
    _stub(monkeypatch, ds=_info(1.0), gt=1.3)
    res = _run(_data_tool().price(TokenRefParams(chain="base", address=TOK)))
    assert "DISPUTED — do not size or value a spend on this" in res.extracted_content
    assert res.metadata["quote"]["confidence"] == "disputed"


def test_price_single_pool_is_shown_as_such(monkeypatch):
    from tools.defi.data_tool import TokenRefParams
    _stub(monkeypatch, ds=_info(1.0, liq=9_000, pools=1, conf="low"))
    res = _run(_data_tool().price(TokenRefParams(chain="base", address=TOK)))
    assert "confidence: single_pool" in res.extracted_content
    assert res.metadata["quote"]["confidence"] == "low"


def test_token_info_carries_typed_facts(monkeypatch):
    from tools.defi.data_tool import TokenRefParams
    from tools.defi.providers.base import HolderReport, ScreenVerdict
    _stub(monkeypatch, ds=_info(1.0), gt=1.3)
    tool = _data_tool(screen_fn=lambda c, a: ScreenVerdict(available=True, checks={"x": "0"},
                                                           missing=["is_honeypot"]),
                      holders_fn=lambda c, a: HolderReport(available=False, reason="n/a"),
                      facts_fn=lambda c, a: [])   # 071 W2: GoPlus is the only source here
    res = _run(tool.token_info(TokenRefParams(chain="base", address=TOK)))
    assert "DISPUTED" in res.extracted_content
    md = res.metadata
    assert md["decimals"] == 18 and md["quote"]["confidence"] == "disputed"
    assert md["screen"]["partial"]
    assert [n["name"] for n in md["screen"]["not_checked"]] == ["is_honeypot"]
    assert md["screen"]["checks"] == [{"name": "x", "result": "0", "source": "goplus"}]


def test_portfolio_metadata_total_and_rows(monkeypatch):
    from tools.defi.data_tool import PortfolioParams
    prices = {TOK: _info(2.0), TOK2: _info(5.0, liq=1_000, pools=1, conf="low")}
    tool = _data_tool(holder="0x" + "aa" * 20,
                      price_fn=lambda c, a: prices[a],
                      balances_fn=lambda h, c, toks: {TOK: 3 * 10 ** 18, TOK2: 10 ** 18},
                      native_fn=lambda h, c: 0.0)
    monkeypatch.setattr(tool, "_scan_set", lambda chain: [TOK, TOK2])
    res = _run(tool.portfolio(PortfolioParams(chain="base")))
    md = res.metadata
    assert md["total_usd"] == pytest.approx(6.0)
    rows = {r["address"]: r for r in md["holdings"]}
    assert rows[TOK]["usd"] == pytest.approx(6.0) and rows[TOK]["amount_human"] == 3.0
    assert rows[TOK2]["status"] == "excluded" and rows[TOK2]["usd"] is None
    assert md["excluded"] == [TOK2]


def test_the_book_reads_worth_from_metadata_first():
    from tools.defi.book import _worth_from_portfolio
    md = {"holdings": [{"address": TOK, "usd": 6.0, "status": "valued"},
                       {"address": TOK2, "usd": None, "status": "excluded"}]}
    # The prose says something else; the typed fact wins.
    text = f"  {TOK}  3.0 TOK  = $999.00"
    assert _worth_from_portfolio(text, TOK, metadata=md) == 6.0
    assert _worth_from_portfolio(text, TOK2, metadata=md) is None
    assert _worth_from_portfolio(text, TOK) == 999.0          # no metadata: regex


# -- GoPlus: one answer for screen + holders -----------------------------------

def test_goplus_screen_and_holders_share_one_http_answer(monkeypatch):
    from tools.defi.providers import _http, goplus
    calls = []
    def answer(url, **kwargs):
        calls.append(url)
        return {"code": 1, "result": {TOK.lower(): {"holder_count": "10", "holders": []}}}
    monkeypatch.setattr(_http, "get_json", answer)
    goplus.screen("base", TOK)
    goplus.holders("base", TOK)
    assert len(calls) == 1


def test_goplus_does_not_cache_a_failure(monkeypatch):
    from tools.defi.providers import _http, goplus
    calls = []
    def answer(url, **kwargs):
        calls.append(url)
        raise _http.HttpStatusError(url, 429)
    monkeypatch.setattr(_http, "get_json", answer)
    assert goplus.screen("base", TOK).available is False
    goplus.holders("base", TOK)
    assert len(calls) == 2


def test_a_goplus_error_body_is_not_cached(monkeypatch):
    from core.intel import cache as intel_cache
    from tools.defi.providers import goplus, _http
    intel_cache.clear_all()
    calls = []
    def answer(url, **kwargs):
        calls.append(url)
        return {"code": 4029, "message": "too many requests", "result": None}
    monkeypatch.setattr(_http, "get_json", answer)
    goplus._fetch("https://x/api", TOK, 5.0)
    goplus._fetch("https://x/api", TOK, 5.0)
    assert len(calls) == 2


def test_goplus_refuses_an_answer_for_another_token(monkeypatch):
    from core.intel import cache as intel_cache
    from tools.defi.providers import goplus, _http
    intel_cache.clear_all()
    monkeypatch.setattr(_http, "get_json", lambda *a, **kw: {
        "code": 1, "result": {TOK2: {"is_honeypot": "0", "sell_tax": "0"}}})
    assert not goplus.screen("base", TOK).available
