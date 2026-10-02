"""Price: from one source to an agreed quote (071 §3.3, R5/R6).

Before this, seven call sites each asked DexScreener and nothing checked the
number against anything. Now every price goes through :func:`quote`:

* every registered source that covers the chain is asked (or its answer is
  handed in, already fetched in a batch);
* the quote is the MEDIAN of the sources that answered;
* sources more than 2 % apart (a liquid pool, >= $1M) or 5 % apart (a thin
  one) earn a NOTE and keep their grade; only a gap above 10 % (liquid) or
  25 % (thin) makes the quote ``disputed`` — shown, never used to size or value
  a spend (eased 2026-10-02: indexers lag each other by a few percent on every
  moving token, and the strict bands blocked ordinary trades);
* with three answers, one OUTLIER secondary that the other two outvote is
  named and does not dispute the quote (the primary is never excused — the
  spend path uses its number);
* a source that failed is NAMED in ``failed`` — never a zero, never "no pool".

The grade is only ever made STRICTER here. ``high`` comes from the primary
pool source's own rule (>= 2 pools AND >= $50k in the priced pool,
``tools/defi/providers/base.py``) and nothing else; a secondary source can
take it away (dispute) and can never grant it. A price that only a secondary
source supplied is ``low``.

The sources live in the tools tier and register here (``core`` must not import
``tools``). With none registered, every quote is ``unknown`` and says why.
"""
from __future__ import annotations

import math
import statistics
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from core.intel.cache import cache_for
from core.intel.model import DISPUTED, HIGH, LOW, UNKNOWN, PriceQuote, SourceAnswer

#: A pool at or above this priced depth is "liquid".
LIQUID_USD = 1_000_000.0
#: Above these the sources' gap is NOTED (the grade is kept).
NOTE_PCT_LIQUID = 2.0
NOTE_PCT_THIN = 5.0
#: Above these the quote is DISPUTED and never sizes or values a spend.
DISPUTE_PCT_LIQUID = 10.0
DISPUTE_PCT_THIN = 25.0


@dataclass(frozen=True)
class Source:
    name: str
    fn: Callable[[str, str], SourceAnswer]
    covers: Callable[[str], bool]
    #: The pool source whose grade (high/low/unknown) the quote starts from.
    primary: bool = False


_SOURCES: List[Source] = []
_LOCK = threading.Lock()


def register_source(name: str, fn: Callable[[str, str], SourceAnswer], *,
                    covers: Optional[Callable[[str], bool]] = None,
                    primary: bool = False) -> None:
    """Register (or replace, by name) a price source. Idempotent."""
    src = Source(name=name, fn=fn, covers=covers or (lambda chain: True), primary=primary)
    with _LOCK:
        for i, s in enumerate(_SOURCES):
            if s.name == name:
                _SOURCES[i] = src
                break
        else:
            _SOURCES.append(src)
        # The primary is asked first so its answer leads every render.
        _SOURCES.sort(key=lambda s: (not s.primary,))


def registered_sources() -> Tuple[Source, ...]:
    with _LOCK:
        return tuple(_SOURCES)


def unregister_all() -> None:
    """Tests only."""
    with _LOCK:
        _SOURCES.clear()


def _finite_positive(value) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if (math.isfinite(out) and out > 0) else None


def cache_key(chain: str, address: str) -> Tuple[str, str]:
    """EVM addresses are case-insensitive; base58 is case-SENSITIVE."""
    chain = str(chain or "").strip().lower()
    addr = str(address or "").strip()
    try:
        from core.wallet import chains
        row = chains.get(chain)
        if row is None or row.family == "evm":
            addr = addr.lower()
    except Exception:
        addr = addr.lower() if addr.startswith("0x") else addr
    return chain, addr


def _liquid(priced_liquidity_usd: Optional[float]) -> bool:
    return priced_liquidity_usd is not None and priced_liquidity_usd >= LIQUID_USD


def dispute_threshold_pct(priced_liquidity_usd: Optional[float]) -> float:
    return DISPUTE_PCT_LIQUID if _liquid(priced_liquidity_usd) else DISPUTE_PCT_THIN


def note_threshold_pct(priced_liquidity_usd: Optional[float]) -> float:
    return NOTE_PCT_LIQUID if _liquid(priced_liquidity_usd) else NOTE_PCT_THIN


def _spread(values: List[float]) -> Optional[float]:
    if len(values) < 2:
        return None
    mid = statistics.median(values)
    return (max(values) - min(values)) / mid * 100.0 if mid else None


def _outlier(priced: List[Tuple[str, float]], primary_name: Optional[str],
             note_pct: float) -> Optional[Tuple[str, float, float]]:
    """``(name, value, pct_off)`` of ONE secondary the others outvote, else None.
    Needs >= 3 answers, the rest agreeing within the note band, and never
    excuses the primary (its number is the spend number)."""
    if len(priced) < 3:
        return None
    mid = statistics.median(v for _, v in priced)
    if not mid:
        return None
    name, value = max(priced, key=lambda nv: abs(nv[1] - mid))
    if name == primary_name:
        return None
    rest = [v for n, v in priced if n != name]
    rest_spread = _spread(rest)
    if rest_spread is None or rest_spread > note_pct:
        return None
    return name, value, (value - statistics.median(rest)) / statistics.median(rest) * 100.0


def aggregate(answers: Iterable[SourceAnswer], *, now: Optional[float] = None) -> PriceQuote:
    """Pure: several source answers -> one :class:`PriceQuote`."""
    answers = list(answers)
    primary = next((a for a in answers if a.confidence is not None), None)
    priced: List[Tuple[str, float]] = []
    failed: List[Tuple[str, str]] = []
    silent: List[str] = []
    for a in answers:
        if a.error:
            failed.append((a.name, a.error))
            continue
        usd = _finite_positive(a.usd)
        if usd is None:
            silent.append(a.name)
        else:
            priced.append((a.name, usd))

    p_usd = _finite_positive(primary.usd) if primary is not None and not primary.error else None
    p_conf = (primary.confidence or UNKNOWN) if p_usd is not None else UNKNOWN
    pool_src = primary if p_usd is not None else None
    if pool_src is None:
        # No primary price: depth facts come from whichever source supplied them.
        pool_src = next((a for a in answers if not a.error and _finite_positive(a.usd)
                         and (a.liquidity_usd is not None or a.pool)), None)

    liq = pool_src.liquidity_usd if pool_src is not None else None
    priced_liq = (pool_src.priced_liquidity_usd if pool_src is not None
                  and pool_src.priced_liquidity_usd is not None else liq)
    pool = pool_src.pool if pool_src is not None else None
    pool_count = int(pool_src.pool_count or 0) if pool_src is not None else 0
    stamp = time.monotonic() if now is None else now

    if not priced:
        why = []
        if silent:
            why.append(f"no priced pool on {', '.join(silent)}")
        if failed:
            why.append("did not answer: " + ", ".join(f"{n} ({e})" for n, e in failed))
        if not answers:
            why.append("no price source covers this chain")
        return PriceQuote(usd=None, confidence=UNKNOWN, reason="; ".join(why) or "no price",
                          failed=tuple(failed), liquidity_usd=liq, pool=pool,
                          pool_count=pool_count, priced_liquidity_usd=priced_liq,
                          fetched_at=stamp)

    values = [v for _, v in priced]
    median = statistics.median(values)
    spread = _spread(values)
    threshold = dispute_threshold_pct(priced_liq)
    note_at = note_threshold_pct(priced_liq)
    note = None
    judged = spread
    odd = _outlier(priced, primary.name if p_usd is not None else None, note_at)
    if odd is not None and spread is not None and spread > note_at:
        name, value, off = odd
        judged = _spread([v for n, v in priced if n != name])
        median = statistics.median([v for n, v in priced if n != name])
        note = (f"{name} is an outlier ({off:+.1f}% vs the other sources) and was "
                f"outvoted")

    if p_usd is not None:
        confidence = p_conf if p_conf in (HIGH, LOW, UNKNOWN) else LOW
        reason = f"graded by {primary.name}"
    else:
        # A price nobody's pool rule graded is never "high".
        confidence = LOW
        reason = ("no primary pool price; priced from " + ", ".join(n for n, _ in priced))
    if judged is not None and judged > threshold:
        confidence = DISPUTED
        reason = (f"sources disagree by {judged:.2f}% (> {threshold:g}% allowed for "
                  f"{'a liquid' if threshold == DISPUTE_PCT_LIQUID else 'a thin'} pool)")
    elif judged is not None and judged > note_at:
        note = note or (f"sources differ by {judged:.1f}% — within the {threshold:g}% "
                        f"tolerance, so the price stands")
        reason += f"; {note}"
    elif note is not None:
        reason += f"; {note}"
    elif spread is not None:
        reason += f"; {len(values)} sources agree within {spread:.2f}%"
    if failed:
        reason += "; did not answer: " + ", ".join(n for n, _ in failed)

    return PriceQuote(
        usd=median, sources=tuple(priced), spread_pct=spread, liquidity_usd=liq, note=note,
        pool=pool, pool_count=pool_count, confidence=confidence, reason=reason,
        priced_liquidity_usd=priced_liq, primary_usd=p_usd, primary_confidence=p_conf,
        primary_liquidity_usd=(primary.liquidity_usd if primary is not None else None),
        failed=tuple(failed), fetched_at=stamp)


def _ask(src: Source, chain: str, address: str) -> SourceAnswer:
    try:
        got = src.fn(chain, address)
    except Exception as exc:  # a source that raised did not answer
        return SourceAnswer(name=src.name, error=exc.__class__.__name__)
    if got is None:
        return SourceAnswer(name=src.name, error="no answer")
    return got


def quote(chain: str, address: str, *,
          answers: Optional[Mapping[str, SourceAnswer]] = None,
          use_cache: bool = True,
          clock: Callable[[], float] = time.monotonic) -> PriceQuote:
    """The agreed price for ``(chain, address)``.

    ``answers`` carries source answers already fetched (a batch prefetch); a
    source named there is not asked again. A quote is cached for 20 s only when
    every source answered — a failure must not outlive itself.
    """
    key = cache_key(chain, address)
    cache = cache_for("price")
    if use_cache:
        hit = cache.get_with_age(key)
        if hit is not None:
            q, age = hit
            return replace(q, age_s=age)
    given = dict(answers or {})
    todo: List[Source] = []
    for src in registered_sources():
        try:
            if src.covers(key[0]):
                todo.append(src)
        except Exception:
            continue
    # 071 review: ask the sources IN PARALLEL — sequentially, three slow
    # indexers stacked up to ~40 s per reading under the 60 s action guard.
    pending = [s for s in todo if s.name not in given]
    asked: Dict[str, SourceAnswer] = {}
    if len(pending) == 1:
        asked[pending[0].name] = _ask(pending[0], chain, address)
    elif pending:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(pending)) as pool:
            futs = {s.name: pool.submit(_ask, s, chain, address) for s in pending}
            for name, fut in futs.items():
                try:
                    asked[name] = fut.result()
                except Exception as exc:
                    asked[name] = SourceAnswer(name=name, error=exc.__class__.__name__)
    got: List[SourceAnswer] = [given[s.name] if s.name in given else asked[s.name]
                               for s in todo]
    q = aggregate(got, now=clock())
    if use_cache and not q.failed and got:
        cache.put(key, q)
    return q


# -- the three readings every caller needs --------------------------------

def spend_price(chain: str, address: str) -> Optional[float]:
    """A price that may BOUND a cap: the primary pool price, only when the
    quote is ``high`` (a dispute is not high). Same number as before 071; the
    second source can only remove it. Read FRESH (no cache): before 071 the
    guard asked live every time, and a 20 s-old number must not bound a cap."""
    q = quote(chain, address, use_cache=False)
    return q.primary_usd if q.confidence == HIGH else None


def exit_price(chain: str, address: str) -> Optional[float]:
    """The weaker price for the guard's exit exemption (028): the primary pool
    price with MEASURED depth behind it, any grade but unknown.

    071 review: a dispute does NOT remove it. A falling thin token is exactly
    when indexers lag each other, and refusing the stop-loss sell then is the
    worst outcome; selling out of a position only reduces exposure. This is the
    pre-071 rule, unchanged. Read fresh, like :func:`spend_price`."""
    q = quote(chain, address, use_cache=False)
    if q.primary_usd is None or q.primary_confidence == UNKNOWN:
        return None
    if not q.primary_liquidity_usd or q.primary_liquidity_usd <= 0:
        return None
    return q.primary_usd


def indexer_price(chain: str, address: str) -> Optional[float]:
    """The primary pool price at any grade (the launchpad / dapp / agent-NFT
    gas readers used the raw DexScreener price verbatim), except
    that a DISPUTED quote yields None — it is never used to value a spend."""
    q = quote(chain, address)
    return None if q.disputed else q.primary_usd
