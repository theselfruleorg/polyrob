"""GeckoTerminal — pool DISCOVERY (keyless). Proposal 029 R4.

`defi_data` could answer questions about a token you already knew about, but it
had no verb for "what launched in the last hour". So the prod agent did
discovery by hand: `web_fetch` against DexScreener's `token-profiles/latest` and
GeckoTerminal's `new_pools`, parsing the JSON in-context, every run. That is why
403s and "page params 400" ended up in its reports — it was scraping, not
calling a provider.

Parsing is kept strictly separate from fetching so tests never touch the
network, mirroring `providers/dexscreener.py`.

**Everything here is a CLAIM, and the claims are unusually weak.** These are
pools minutes old. Anyone can create one. Two failure modes are common enough to
be handled explicitly rather than left to the caller:

* **Reserves come back non-positive.** In one live page, 9 of 20 rows reported a
  negative `reserve_in_usd` — an indexing artifact on a pool the indexer has not
  caught up with. A non-positive reserve is UNKNOWN here, never `0.0`, because
  "$0 liquidity" and "the indexer has not indexed it" are different facts and
  only one of them is a reason to reject a token.
* **The base token may not be on the chain you asked about.** The `base_token`
  relationship id is prefixed with the network (`base_0x…`), so a row whose
  prefix disagrees is dropped rather than trusted.

This module ranks nothing and rejects nothing on merit. It returns what the
indexer says; the honeypot/sell-tax screens (`providers/goplus.py`) and the
caller's own liquidity floors do the deciding.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

BASE_URL = "https://api.geckoterminal.com/api/v2"
TIMEOUT_SEC = 12.0

name = "geckoterminal"


def _usable_denominator(value) -> bool:
    """A denominator must be a finite POSITIVE number.

    `_positive()` keeps a negative reserve out of the parser, but the dataclass
    is public and a negative denominator silently produced a NEGATIVE V/L —
    which then classified as PASS rather than as unreadable. A ratio computed
    from a figure that cannot be a liquidity is not a measurement.
    """
    import math
    try:
        return value is not None and math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class PoolTrades:
    """Trade counts for ONE time window, as the indexer reports them.

    ``buyers``/``sellers`` are UNIQUE addresses; ``buys``/``sells`` are
    transactions. The ratio between them is what separates real participation
    from two bots passing a token back and forth, so both are kept.
    """
    buys: Optional[int] = None
    sells: Optional[int] = None
    buyers: Optional[int] = None
    sellers: Optional[int] = None


@dataclass(frozen=True)
class PoolCandidate:
    """One freshly-indexed pool. Never "a good token" — just a pool that exists."""
    chain: str
    pool_address: str
    #: The non-quote side, checksummed. ``None`` when the indexer did not name a
    #: token this chain's address rules accept — the row is still shown, because
    #: hiding it would misreport how much the scan actually saw.
    base_token: Optional[str]
    name: str
    dex: str
    created_at: Optional[str]
    #: ``None`` = the indexer gave no usable figure. NOT zero. See the module
    #: docstring: a negative reserve is an indexing artifact, not an empty pool.
    liquidity_usd: Optional[float]
    volume_h24_usd: Optional[float]
    price_change_h24_pct: Optional[float]
    #: Everything below arrives in the SAME payload and used to be discarded.
    volume_h1_usd: Optional[float] = None
    market_cap_usd: Optional[float] = None
    fdv_usd: Optional[float] = None
    trades_h24: Optional["PoolTrades"] = None
    trades_h1: Optional["PoolTrades"] = None

    # -- derived reads. Every one returns None rather than a number it cannot
    # honestly compute; a ratio invented from a missing denominator is worse
    # than no ratio, because it looks like a measurement.

    @property
    def vol_liq_ratio(self) -> Optional[float]:
        """24h volume ÷ pool liquidity — the single strongest separator measured
        on this chain (winners 0.06-2.1, wash cases 13-124)."""
        if not _usable_denominator(self.liquidity_usd) or self.volume_h24_usd is None:
            return None
        return self.volume_h24_usd / self.liquidity_usd

    @property
    def hourly_vol_liq_ratio(self) -> Optional[float]:
        """1h volume ÷ liquidity. Above ~5 is a wash IN PROGRESS, which the 24h
        figure smears out."""
        if not _usable_denominator(self.liquidity_usd) or self.volume_h1_usd is None:
            return None
        return self.volume_h1_usd / self.liquidity_usd

    @property
    def mcap_liq_ratio(self) -> Optional[float]:
        """Market cap ÷ liquidity. Market cap is not realizable money; this says
        how far the two have separated."""
        if not _usable_denominator(self.liquidity_usd) or self.market_cap_usd is None:
            return None
        return self.market_cap_usd / self.liquidity_usd

    @property
    def txns_per_buyer(self) -> Optional[float]:
        """24h transactions per UNIQUE buyer. Bot ping-pong ran 25-32; every
        measured winner stayed under 15."""
        t = self.trades_h24
        if t is None or not t.buyers:
            return None
        total = (t.buys or 0) + (t.sells or 0)
        return total / t.buyers if total else None

    @property
    def buy_sell_ratio(self) -> Optional[float]:
        t = self.trades_h24
        if t is None or not t.sells or t.buys is None:
            return None
        return t.buys / t.sells

    @property
    def age_hours(self) -> Optional[float]:
        if not self.created_at:
            return None
        from datetime import datetime, timezone
        try:
            dt = datetime.fromisoformat(str(self.created_at).replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0


def _f(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def _positive(value: Any) -> Optional[float]:
    """A figure that is only meaningful when positive; otherwise UNKNOWN."""
    out = _f(value)
    return out if (out is not None and out > 0) else None


def _base_token(rel: Dict[str, Any], gt_network: str, chain: str) -> Optional[str]:
    """The base token's address, or None.

    The id is ``<network>_<address>``. The network prefix is CHECKED rather than
    stripped blindly: a row for another chain would otherwise be reported as if
    it were on the one the caller asked about, and the same address is a
    different token on a different chain.
    """
    raw = (((rel or {}).get("base_token") or {}).get("data") or {}).get("id") or ""
    prefix = f"{gt_network}_"
    if not raw.startswith(prefix):
        return None
    addr = raw[len(prefix):]
    # Family-dispatched: a Solana mint is base58 and would fail EIP-55, so an
    # EVM-only validator here silently blanked every Solana row.
    from core.wallet.addresses import normalize_for_chain
    try:
        return normalize_for_chain(chain, addr)
    except ValueError:
        logger.debug("geckoterminal: dropping malformed base token %r", addr)
        return None


@dataclass(frozen=True)
class Candle:
    """One OHLCV bar. ``timestamp`` is unix seconds at the bar's OPEN."""
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume_usd: Optional[float] = None


@dataclass(frozen=True)
class CandleSummary:
    """The shape a 24h snapshot cannot show.

    Every field is Optional and stays None on an empty series: a chart nobody
    sent is not a flat chart.
    """
    count: int = 0
    first: Optional[float] = None
    last: Optional[float] = None
    peak: Optional[float] = None
    trough: Optional[float] = None
    candles_to_peak: Optional[int] = None
    pct_off_peak: Optional[float] = None
    pct_from_first: Optional[float] = None
    peak_in_first_half: Optional[bool] = None


def parse_ohlcv(payload: Any) -> List[Candle]:
    """Pure. Oldest-first, malformed rows DROPPED rather than zero-filled.

    The indexer returns newest-first; a series read in the wrong direction
    inverts "climbed for weeks" into "dumped for weeks", so the order is
    normalised here rather than at each call site.
    """
    rows = (((payload or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") \
        if isinstance(payload, dict) else None
    out: List[Candle] = []
    for row in (rows or []):
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        try:
            candle = Candle(int(row[0]), float(row[1]), float(row[2]),
                            float(row[3]), float(row[4]),
                            float(row[5]) if len(row) > 5 and row[5] is not None else None)
        except (TypeError, ValueError):
            continue
        out.append(candle)
    out.sort(key=lambda c: c.timestamp)
    return out


def summarize_candles(candles: List[Candle]) -> CandleSummary:
    """Pure. Where the peak sits in the series is the separator.

    A token that peaked in the first half and sits far below it round-tripped;
    one that peaks late and holds near it is still climbing. That distinction is
    invisible in a 24h change figure, and it is the one the measured winner /
    wash split turns on.
    """
    if not candles:
        return CandleSummary()
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    peak = max(highs)
    peak_index = highs.index(peak)
    last = closes[-1]
    first = closes[0]
    return CandleSummary(
        count=len(candles),
        first=first, last=last, peak=peak, trough=min(c.low for c in candles),
        candles_to_peak=peak_index,
        pct_off_peak=((last - peak) / peak * 100.0) if peak else None,
        pct_from_first=((last - first) / first * 100.0) if first else None,
        peak_in_first_half=peak_index < (len(candles) - 1) / 2.0,
    )


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _trades(raw: Any, window: str) -> Optional[PoolTrades]:
    """One window's counts, or None when the indexer sent nothing for it.

    None, not a zeroed PoolTrades: "no transactions" and "the indexer did not
    say" are different facts, and only one of them is a reason to reject a pool.
    """
    if not isinstance(raw, dict):
        return None
    row = raw.get(window)
    if not isinstance(row, dict):
        return None
    return PoolTrades(buys=_int(row.get("buys")), sells=_int(row.get("sells")),
                      buyers=_int(row.get("buyers")), sellers=_int(row.get("sellers")))


def parse_pools(payload: Any, chain: str, gt_network: str) -> List[PoolCandidate]:
    """Pure. ``payload`` is the decoded JSON body; no network here."""
    rows = ((payload or {}).get("data") or []) if isinstance(payload, dict) else []
    out: List[PoolCandidate] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        attrs = row.get("attributes") or {}
        rel = row.get("relationships") or {}
        vol = attrs.get("volume_usd") or {}
        chg = attrs.get("price_change_percentage") or {}
        out.append(PoolCandidate(
            chain=chain,
            pool_address=str(attrs.get("address") or ""),
            base_token=_base_token(rel, gt_network, chain),
            name=str(attrs.get("name") or ""),
            dex=str(((rel.get("dex") or {}).get("data") or {}).get("id") or "unknown"),
            created_at=attrs.get("pool_created_at"),
            liquidity_usd=_positive(attrs.get("reserve_in_usd")),
            volume_h24_usd=_f(vol.get("h24")),
            price_change_h24_pct=_f(chg.get("h24")),
            volume_h1_usd=_f(vol.get("h1")),
            # `_positive`, not `_f`: the indexer sends 0 for a pool it has not
            # valued, and a token with a live pool does not have a market cap of
            # zero. Same artifact class as the negative reserve above.
            market_cap_usd=_positive(attrs.get("market_cap_usd")),
            fdv_usd=_positive(attrs.get("fdv_usd")),
            trades_h24=_trades(attrs.get("transactions"), "h24"),
            trades_h1=_trades(attrs.get("transactions"), "h1"),
        ))
    return out


def _get(url: str) -> Any:
    from tools.defi.providers._http import get_json
    return get_json(url, timeout=TIMEOUT_SEC)


def _fetch(chain: str, endpoint: str, *, fetch=None) -> List[PoolCandidate]:
    from core.wallet import chains
    row = chains.get(chain)
    if row is None or not row.geckoterminal_id:
        # A chain this indexer does not cover returns NOTHING rather than
        # another chain's pools. "We could not look" is the honest answer.
        return []
    url = f"{BASE_URL}/networks/{row.geckoterminal_id}/{endpoint}"
    try:
        payload = (fetch or _get)(url)
    except Exception as exc:
        logger.info("geckoterminal: %s failed for %s (%s)", endpoint, chain, exc)
        return []
    return parse_pools(payload, chain, row.geckoterminal_id)


def new_pools(chain: str, *, fetch=None) -> List[PoolCandidate]:
    """Pools the indexer saw most recently — the fresh-launch frontier."""
    return _fetch(chain, "new_pools", fetch=fetch)


def trending_pools(chain: str, *, fetch=None) -> List[PoolCandidate]:
    """Pools the indexer currently ranks as trending. A popularity claim, and
    popularity is purchasable — treat it as a place to look, not a verdict."""
    return _fetch(chain, "trending_pools", fetch=fetch)


def _chain_for_gt_network(gt_network: str) -> Optional[str]:
    """Our chain name for an indexer network id, or None when we do not cover it.

    The reverse of the registry's ``geckoterminal_id``. A row on a chain this
    tier does not cover is DROPPED rather than renamed, because a candidate we
    cannot price, screen or trade is not a candidate.
    """
    from core.wallet import chains
    for row in chains.all_rows():
        if row.geckoterminal_id == gt_network:
            return row.name
    return None


def parse_search_pools(payload: Any) -> List:
    """Pure. Pool search results as resolver CANDIDATES, across every chain.

    This is the SECOND index behind ``token_resolve``. The first one
    (DexScreener) lags a fresh launch by hours, which in prod meant the resolver
    "only knows wrong-chain or established tokens" — the exact tokens the agent
    is not hunting.

    A pool is not a token: the base side is taken, the quote side ignored, and
    the row is dropped unless the network prefix resolves to a chain we cover.
    """
    from tools.defi.providers.base import Candidate
    rows = ((payload or {}).get("data") or []) if isinstance(payload, dict) else []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        attrs = row.get("attributes") or {}
        raw_id = ((((row.get("relationships") or {}).get("base_token")
                    or {}).get("data") or {}).get("id") or "")
        if "_" not in raw_id:
            continue
        gt_network = raw_id.split("_", 1)[0]
        chain = _chain_for_gt_network(gt_network)
        if chain is None:
            continue
        address = _base_token(row.get("relationships") or {}, gt_network, chain)
        if not address:
            continue
        name = str(attrs.get("name") or "")
        symbol = name.split("/")[0].strip() or None
        out.append(Candidate(
            chain=chain, address=address, symbol=symbol, name=name or None,
            liquidity_usd=_positive(attrs.get("reserve_in_usd")) or 0.0,
            price_usd=_f(attrs.get("base_token_price_usd")),
        ))
    return out


def search_candidates(symbol: str, *, fetch=None) -> List:
    """Ask the pool index for a ticker. Keyless. Raises nothing to the caller
    beyond what ``_get`` raises — the verb decides what a failure means."""
    import urllib.parse
    url = f"{BASE_URL}/search/pools?query={urllib.parse.quote(str(symbol or ''))}"
    return parse_search_pools((fetch or _get)(url))


#: The windows the indexer serves. `minute` exists too but is rarely useful at
#: our cadence and burns the rate limit fastest.
OHLCV_TIMEFRAMES = ("day", "hour", "minute")


def _url_safe_address(chain: str, value: str, *, what: str) -> str:
    """An address that is safe to interpolate into a URL PATH, or ValueError.

    These values reach the indexer as a path SEGMENT. Before this, a caller
    could pass ``../../../networks/eth/trending_pools?x=`` and re-point the
    request at a different endpoint on the same host — whose answer would then
    be rendered as THIS token's history. The agent reads web pages, so an
    address it is told is attacker-influenced input on a money-adjacent path.

    Validated as an ADDRESS for the chain rather than merely escaped: escaping
    would make a nonsense value into a harmless 404, while this says which
    argument was wrong. A pool address has the same shape as any other address
    on its chain.
    """
    from core.wallet.addresses import normalize_for_chain
    text = str(value or "").strip()
    try:
        normalize_for_chain(chain, text)
    except ValueError as exc:
        raise ValueError(
            f"{what} {text!r} is not an address on {chain}: {exc}") from exc
    # The VALIDATED original, not the normalized form. Normalizing would
    # EIP-55-checksum an EVM address, and an indexer that 404s on checksummed
    # input is a real shape (LI.FI does exactly that). Solana is base58 and
    # case-SENSITIVE, so it must not be touched at all. This call needs the
    # value to be an address; it does not need it to be spelled our way.
    return text


def ohlcv(chain: str, pool_address: str, *, timeframe: str = "hour",
          aggregate: int = 1, limit: int = 48, fetch=None) -> List[Candle]:
    """Candles for ONE POOL. Keyless. Empty list on any failure — never a
    fabricated flat series."""
    from core.wallet import chains
    row = chains.get(chain)
    if row is None or not row.geckoterminal_id:
        return []
    pool_address = _url_safe_address(chain, pool_address, what="pool")
    if timeframe not in OHLCV_TIMEFRAMES:
        raise ValueError(f"timeframe {timeframe!r} is not one of "
                         f"{', '.join(OHLCV_TIMEFRAMES)}")
    url = (f"{BASE_URL}/networks/{row.geckoterminal_id}/pools/{pool_address}"
           f"/ohlcv/{timeframe}?aggregate={int(aggregate)}&limit={int(limit)}")
    try:
        return parse_ohlcv((fetch or _get)(url))
    except Exception as exc:
        logger.info("geckoterminal: ohlcv failed for %s/%s (%s)", chain, pool_address, exc)
        return []


def top_pool_for_token(chain: str, token_address: str, *, fetch=None) -> Optional[str]:
    """The DEEPEST pool for a token, because candles are pool-scoped.

    Deepest rather than first: a token's pools disagree, and the thin one is the
    one an attacker seeded. Returns None when nothing is indexed — which is a
    real answer for a token minutes old, not an error to paper over.
    """
    from core.wallet import chains
    row = chains.get(chain)
    if row is None or not row.geckoterminal_id:
        return None
    token_address = _url_safe_address(chain, token_address, what="token")
    url = f"{BASE_URL}/networks/{row.geckoterminal_id}/tokens/{token_address}/pools"
    try:
        payload = (fetch or _get)(url)
    except Exception as exc:
        logger.info("geckoterminal: pools-for-token failed for %s/%s (%s)",
                    chain, token_address, exc)
        return None
    best, best_liq = None, None
    for item in ((payload or {}).get("data") or []):
        attrs = (item or {}).get("attributes") or {}
        addr = str(attrs.get("address") or "").strip()
        if not addr:
            continue
        liq = _positive(attrs.get("reserve_in_usd"))
        if best is None or (liq is not None and (best_liq is None or liq > best_liq)):
            best, best_liq = addr, liq
    return best


def health() -> bool:
    try:
        _get(f"{BASE_URL}/networks/base/new_pools")
        return True
    except Exception:
        return False
