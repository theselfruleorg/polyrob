"""Pool discovery with an honest status, and the hunt's $0 candidate count (086 W9).

On 2026-10-12 six discovery reads on three chains (robinhood, base, solana)
came back empty and the trading hunt reported "0 candidates". Two causes, both
in the plumbing, neither in the market:

* ``geckoterminal._fetch`` turned every provider error into ``[]``, and a burst
  of reads met the indexer's free-tier limit (HTTP 429 after about four calls
  in a burst, measured 2026-10-23). Fixed 2026-10-13: the read now RAISES
  ``DiscoveryUnavailable``; this module keeps that distinction typed.
* ``scan`` with a liquidity floor that excluded every pool rendered "no pools
  were returned for this chain" — the indexer HAD returned them. The renderers
  now say how many pools the indexer sent and how many a floor removed.

So a discovery read has exactly three outcomes, and a caller can always tell
them apart:

* ``ok``          — the indexer answered with at least one pool;
* ``empty``       — the indexer answered with ZERO pools (a real answer);
* ``unavailable`` — the read did not happen (error, rate limit, uncovered
  chain). This is NOT an empty market.

:func:`discovery_candidates` is the primitive behind the W3 read-job verb
(``defi_data_discovery_count``): the fixed reads plus a cheap pre-screen — no
LLM, no contract screen. A pool whose figure is UNKNOWN never passes a gate:
the count answers "is there anything worth waking the model for", and an
unknown is not something.

Token names and symbols are attacker-written; nothing here returns them.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

OK = "ok"
EMPTY = "empty"
UNAVAILABLE = "unavailable"

KINDS = ("new", "trending")

#: The hunt's default pre-screen. The liquidity floor is the Tier C entry
#: floor of the memecoin ladder (``treasury-trading``: $3k); the age window is
#: "new pairs hours–days old" (086 §3.3 ``memecoin``).
DEFAULT_MIN_LIQUIDITY_USD = 3_000.0
DEFAULT_MAX_AGE_H = 72.0
#: A pool minutes old has proven nothing, and the indexer's first figures for
#: it are the least reliable (a 9-minute-old pool read $495M "liquidity" on
#: 2026-10-23). One hour is the bottom of "hours–days old".
DEFAULT_MIN_AGE_H = 1.0
#: A pool nobody trades is not a candidate: most fresh pools show $0–$30 of
#: 24h volume (measured 2026-10-23 on base and robinhood).
DEFAULT_MIN_VOLUME_H24_USD = 1_000.0

#: Market verdicts (``pool_screen``) that end a candidate at the pre-screen:
#: WASH is a reject at any size; UNSCREENABLE means the screen did not run.
_REJECT_VERDICTS = frozenset({"WASH", "UNSCREENABLE"})

DiscoverFn = Callable[[str, str], Sequence[Any]]


@dataclass(frozen=True)
class DiscoveryRead:
    """One discovery read and what happened to it."""
    chain: str
    kind: str
    status: str
    pools: Tuple[Any, ...] = ()
    reason: Optional[str] = None

    @property
    def available(self) -> bool:
        return self.status != UNAVAILABLE


def _default_discover(chain: str, kind: str):
    from tools.defi.providers import geckoterminal
    fn = geckoterminal.new_pools if kind == "new" else geckoterminal.trending_pools
    return fn(chain)


def read(chain: str, kind: str, *, discover_fn: Optional[DiscoverFn] = None) -> DiscoveryRead:
    """One discovery read with a typed outcome. Never raises."""
    from core.wallet import chains
    kind = (kind or "").strip().lower()
    if kind not in KINDS:
        return DiscoveryRead(chain, kind, UNAVAILABLE,
                             reason=f"kind {kind!r} must be 'new' or 'trending'")
    if chains.get(chain) is None:
        return DiscoveryRead(chain, kind, UNAVAILABLE,
                             reason=f"chain {chain!r} is not in the chain registry")
    try:
        pools = tuple((discover_fn or _default_discover)(chain, kind) or ())
    except Exception as exc:
        logger.info("discovery: %s/%s unavailable (%s)", chain, kind, exc)
        return DiscoveryRead(chain, kind, UNAVAILABLE, reason=str(exc) or type(exc).__name__)
    return DiscoveryRead(chain, kind, OK if pools else EMPTY, pools=pools)


def unavailable_text(r: DiscoveryRead) -> str:
    """The error line for an unavailable read — never confusable with 'no pools'."""
    return (f"pool discovery could not read {r.chain}: {r.reason}. This is NOT "
            f"'no pools' — the list was not read.")


def read_metadata(r: DiscoveryRead, *, shown: int = 0, excluded: int = 0) -> Dict[str, Any]:
    """Typed facts for ``ActionResult.metadata`` — the prose is not the interface."""
    return {"chain": r.chain, "kind": r.kind, "status": r.status,
            "provider_pools": len(r.pools), "shown": shown, "excluded": excluded,
            "reason": r.reason}


# --------------------------------------------------------------------------
# The pre-screen and the count
# --------------------------------------------------------------------------

def prescreen(pool, *, min_liquidity_usd: float, max_age_h: float,
              min_age_h: float = 0.0, min_volume_h24_usd: float = 0.0) -> Tuple[bool, str, Any]:
    """``(passed, reason, verdict)`` for one pool. Pure, no network.

    An UNKNOWN figure fails its gate: a count that wakes a paid model run must
    count only what was measured."""
    from tools.defi import pool_screen
    if not getattr(pool, "base_token", None):
        return False, "no usable token address", None
    age = getattr(pool, "age_hours", None)
    if age is None:
        return False, "pool age unknown", None
    if age > max_age_h:
        return False, "older than the window", None
    if age < min_age_h:
        return False, "younger than the window", None
    liq = getattr(pool, "liquidity_usd", None)
    if liq is None:
        return False, "liquidity unknown", None
    if liq < min_liquidity_usd:
        return False, "liquidity below the floor", None
    if min_volume_h24_usd > 0:
        vol = getattr(pool, "volume_h24_usd", None)
        if vol is None:
            return False, "24h volume unknown", None
        if vol < min_volume_h24_usd:
            return False, "24h volume below the floor", None
    verdict = pool_screen.classify(pool)
    if verdict.verdict in _REJECT_VERDICTS:
        return False, f"market verdict {verdict.verdict}", verdict
    return True, "passed", verdict


def _candidate_row(pool, kind: str, verdict) -> Dict[str, Any]:
    # No name and no symbol: they are attacker-written and this row feeds a
    # precheck and a prompt. The address is the identity.
    return {
        "chain": pool.chain,
        "token": pool.base_token,
        "pool": getattr(pool, "pool_address", None) or None,
        "dex": getattr(pool, "dex", None),
        "kind": kind,
        "age_h": None if pool.age_hours is None else round(pool.age_hours, 2),
        "liquidity_usd": pool.liquidity_usd,
        "volume_h24_usd": getattr(pool, "volume_h24_usd", None),
        "vol_liq": getattr(pool, "vol_liq_ratio", None),
        "verdict": getattr(verdict, "verdict", None),
    }


def discovery_candidates(
        chains: Iterable[str], *,
        kinds: Sequence[str] = KINDS,
        max_age_h: float = DEFAULT_MAX_AGE_H,
        min_age_h: float = DEFAULT_MIN_AGE_H,
        min_liquidity_usd: float = DEFAULT_MIN_LIQUIDITY_USD,
        min_volume_h24_usd: float = DEFAULT_MIN_VOLUME_H24_USD,
        exclude_tokens: Iterable[Tuple[str, str]] = (),
        discover_fn: Optional[DiscoverFn] = None) -> Dict[str, Any]:
    """Count the discovery candidates that pass the cheap pre-screen. $0, no LLM.

    Returns::

        {"count": int,                       # distinct (chain, token) that passed
         "candidates": [{chain, token, pool, dex, kind, age_h, liquidity_usd,
                         volume_h24_usd, vol_liq, verdict}, ...],  # deepest first
         "unavailable_chains": [chain, ...], # ANY read on the chain failed
         "complete": bool,                   # no read failed
         "reads": [{chain, kind, status, provider_pools, passed, reason}, ...],
         "rejected": {reason: n}}

    A chain in ``unavailable_chains`` makes ``count`` a LOWER BOUND, never a
    zero to act on: 086 §3.6 — a hunt refuses to start while the discovery read
    for a chain is UNAVAILABLE. ``exclude_tokens`` takes ``(chain, address)``
    pairs (held positions, the same-token cooldown) and drops them by address.
    """
    excluded = {(c, str(a).lower()) for c, a in exclude_tokens}
    best: Dict[Tuple[str, str], Dict[str, Any]] = {}
    reads: List[Dict[str, Any]] = []
    unavailable: List[str] = []
    rejected: Dict[str, int] = {}
    for chain in dict.fromkeys(chains):
        for kind in kinds:
            r = read(chain, kind, discover_fn=discover_fn)
            passed = 0
            if not r.available and chain not in unavailable:
                unavailable.append(chain)
            for pool in r.pools:
                ok, why, verdict = prescreen(
                    pool, min_liquidity_usd=min_liquidity_usd, max_age_h=max_age_h,
                    min_age_h=min_age_h, min_volume_h24_usd=min_volume_h24_usd)
                if ok and (chain, str(pool.base_token).lower()) in excluded:
                    ok, why = False, "excluded (held or cooling down)"
                if not ok:
                    rejected[why] = rejected.get(why, 0) + 1
                    continue
                passed += 1
                key = (chain, str(pool.base_token).lower())
                row = _candidate_row(pool, kind, verdict)
                prev = best.get(key)
                if prev is None or (row["liquidity_usd"] or 0) > (prev["liquidity_usd"] or 0):
                    best[key] = row
            reads.append({"chain": chain, "kind": r.kind, "status": r.status,
                          "provider_pools": len(r.pools), "passed": passed,
                          "reason": r.reason})
    candidates = sorted(best.values(), key=lambda c: -(c["liquidity_usd"] or 0))
    return {"count": len(candidates), "candidates": candidates,
            "unavailable_chains": unavailable, "complete": not unavailable,
            "reads": reads, "rejected": rejected}
