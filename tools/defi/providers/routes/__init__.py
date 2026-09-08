"""The route-provider seam — how a swap finds a path, per chain (proposal 029).

Before this package, `trade_tool.swap` called `providers/univ3.py` directly, so
the rail spoke Uniswap V3 and nothing else. Every fresh Base launch that pooled
on Aerodrome or a V2 fork was simply unreachable: `best_quote` returned `None`
and that was the end of the road. The prod agent filed "a V2/Aerodrome-capable
swap path" as its #1 standing owner ask on 15+ consecutive runs while screening
candidates it could never buy.

The fix is an ordered list of providers rather than one hard-coded DEX:

  1. **``univ3``** — the existing local-construction path. Tried FIRST wherever
     it exists, because it is the strongest trust position available: the quote
     comes from the same contracts that execute it, and no third party can
     return a number the pools do not support.
  2. **``lifi``** — a third-party aggregator, consulted ONLY when local
     construction finds no pool. Off by default (``DEFI_ROUTE_AGGREGATOR``).

Trust semantics follow ``providers/base.py``: a quote **fails open to ``None``**
— "no route" renders as unknown, never as a zero-output trade.

## Why a third party's opaque calldata is admissible at all

``core/wallet/tx_guard.py`` never reads calldata. It simulates the transaction
(``eth_simulateV1``) and asserts the holder's OBSERVED balance and allowance
deltas against a declared intent. That model is calldata-agnostic, which is
exactly what makes an aggregator route bounded rather than trusted.

But three properties Uniswap V3 gave us *implicitly* must be made explicit here,
and this module is where they live:

* **The spender is a pinned address.** With V3 it came from the ``ChainRow``.
  A third party names its own, so it is checked against ``row.aggregator_spender``
  for THAT chain. An unpinned spender is refused — approving funds to an address
  nobody verified is the whole attack.
* **The output floor is OURS.** A provider's own minimum is accepted only after
  re-deriving ours from the quote and the caller's slippage, and taking the
  LOWER of the two. A provider can tighten the floor; it can never loosen it.
* **The call target may differ from the spender.** ``to`` and ``spender`` are
  separate fields precisely so a caller cannot conflate them — wiring one where
  the other belongs is the class of mistake ``core/wallet/chains.py`` exists to
  prevent.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace
from typing import Optional, Protocol, Sequence, runtime_checkable

logger = logging.getLogger(__name__)

#: Aggregator names an operator may name in DEFI_ROUTE_AGGREGATOR. An unknown
#: name is REFUSED rather than guessed at — a typo must not silently disarm the
#: aggregator, and it must never resolve to some other provider.
KNOWN_AGGREGATORS = ("lifi",)

_FALSEY = {"", "none", "off", "false", "0", "no"}


@dataclass(frozen=True)
class RouteQuote:
    """One routable path, ready to broadcast.

    ``quoted_at`` exists so the caller can enforce a freshness window: an old
    quote is a stale price, and executing against it is how a swap silently eats
    slippage. An aggregator quote is MORE perishable than a pool read, not less.
    """
    chain: str
    token_in: str
    token_out: str
    amount_in_raw: int
    amount_out_raw: int
    #: The floor this swap must not go below. Set by ``best_route``, never taken
    #: from the provider unchanged.
    amount_out_min_raw: int
    #: What must hold the ERC-20 allowance. NOT necessarily ``to``.
    spender: Optional[str]
    #: The call target.
    to: str
    #: Opaque to us on purpose — the guard asserts the outcome, not the path.
    calldata: str
    value_raw: int
    #: Human-readable provenance for the audit row and the agent's header,
    #: e.g. "uniswap-v3 fee 3000" or "lifi:kyberswap".
    venue: str
    quoted_at: float
    #: True when WE built the calldata from pinned addresses. A locally-built
    #: route needs no aggregator-spender pin; a third-party one always does.
    locally_built: bool = False


@runtime_checkable
class RouteProvider(Protocol):
    name: str

    def supports(self, chain: str) -> bool:
        ...

    def quote(self, chain: str, token_in: str, token_out: str,
              amount_in_raw: int, *, holder: str,
              slippage_bps: int) -> Optional[RouteQuote]:
        ...


def providers() -> Sequence[RouteProvider]:
    """The live provider chain, in the order they are tried.

    ``univ3`` is unconditional. The aggregator joins only when an operator names
    a KNOWN one, so shipping this seam changes nothing until the flag is
    flipped, and ``DEFI_ROUTE_AGGREGATOR=off`` is a one-line rollback.
    """
    from tools.defi.providers.routes.univ3_route import UniV3RouteProvider
    live: list = [UniV3RouteProvider()]

    choice = os.getenv("DEFI_ROUTE_AGGREGATOR", "").strip().lower()
    if choice in _FALSEY:
        return tuple(live)
    if choice not in KNOWN_AGGREGATORS:
        logger.warning(
            "DEFI_ROUTE_AGGREGATOR=%r is not a known aggregator (%s) — no "
            "aggregator route is live. A typo must not silently arm or disarm a "
            "money path, so this refuses rather than guessing.",
            choice, ", ".join(KNOWN_AGGREGATORS))
        return tuple(live)
    if choice == "lifi":
        from tools.defi.providers.routes.lifi import LifiRouteProvider
        live.append(LifiRouteProvider())
    return tuple(live)


#: Captured at import so ``best_route``'s ``providers=`` parameter can shadow
#: the public function name without losing access to it.
_default_providers = providers


def _evm_address(value) -> Optional[str]:
    """*value* as a checksummed EVM address, or ``None`` if it is not one.

    Provider-supplied addresses reach ``rail.build_call`` and the guard's
    ``watch_spenders``, so they are validated HERE rather than trusted because a
    later gate would probably catch the consequence. "Probably" is not the bar
    on a money path.
    """
    if not isinstance(value, str) or not value:
        return None
    from core.wallet.tokens import normalize_address
    try:
        return normalize_address(value)
    except ValueError:
        return None


def _addresses_ok(chain: str, quote: RouteQuote) -> bool:
    """Is this quote's spender AND call target one we are willing to touch?

    Two separate questions that used to be one:

    * **Well-formed.** Both must parse as EVM addresses. A provider returning
      junk is misbehaving; handing that junk to the rail is our mistake.
    * **Pinned.** A locally built route's spender IS the registry's own router,
      already verified where every other address on the row was. A third-party
      route must match ``row.aggregator_spender`` exactly (case-insensitively —
      providers are inconsistent about EIP-55), **and so must its call target**.
      Pinning the spender alone left the transaction free to be sent anywhere:
      the allowance could not follow it, and the guard's delta assertion would
      still have caught a bad outcome, but "the last gate catches it" is not a
      reason to send a call to an address nobody verified.
    """
    from core.wallet import chains
    row = chains.get(chain)
    if row is None:
        return False
    spender = _evm_address(quote.spender)
    target = _evm_address(quote.to)
    if spender is None or target is None:
        return False
    if quote.locally_built:
        return spender.lower() == (row.univ3_router or "").lower()
    pinned = (row.aggregator_spender or "").lower()
    if not pinned:
        return False
    return spender.lower() == pinned and target.lower() == pinned


def _calldata_ok(calldata) -> bool:
    """Opaque is fine; malformed is not. At minimum it must be a 4-byte
    selector's worth of real hex — anything shorter cannot be a call, and
    non-hex would fail at the rail with a far less legible error."""
    if not isinstance(calldata, str) or not calldata.startswith("0x"):
        return False
    body = calldata[2:]
    if len(body) < 8 or len(body) % 2:
        return False
    try:
        bytes.fromhex(body)
    except ValueError:
        return False
    return True


#: Rounding grace when checking a third party's own minimum against ours.
#: Measured, not guessed: LI.FI's ``toAmountMin`` matched our floor to within
#: ±1 raw unit across 50/150/200 bps probes (2026-08-24). One basis point of the
#: quote is far wider than that rounding and far tighter than any loosening that
#: would matter, so a rounding difference does not refuse a good route and a
#: real one still does.
_FLOOR_GRACE_BPS = 1


def _verified_floor(quote: "RouteQuote", slippage_bps: int) -> Optional[int]:
    """The output floor this route will actually be held to, or ``None`` to refuse.

    The asymmetry here is the important part, and it is not symmetric by
    accident:

    * For a **locally built** route WE encode ``amountOutMinimum`` into the
      calldata, so our floor is the floor. Nothing to verify.
    * For a **third-party** route the minimum is already baked into calldata we
      did not write and cannot change. So our floor is not something we can
      *impose* — it is a bar the provider's own minimum must CLEAR. A route
      whose encoded minimum is looser than the caller's slippage tolerance is
      REFUSED, because executing it would accept a worse fill than the caller
      agreed to and there is no lever left to prevent that at broadcast time.

    A third-party route that reports no minimum at all is refused for the same
    reason: an unverifiable floor is not a floor.
    """
    ours = (quote.amount_out_raw * (10_000 - slippage_bps)) // 10_000
    if ours <= 0:
        return None
    if quote.locally_built:
        return ours
    theirs = quote.amount_out_min_raw
    if theirs is None or theirs <= 0:
        return None
    grace = (quote.amount_out_raw * _FLOOR_GRACE_BPS) // 10_000
    if theirs < ours - grace:
        return None
    return theirs


def best_route_with_reason(chain: str, token_in: str, token_out: str,
                           amount_in_raw: int, *, holder: str, slippage_bps: int,
                           providers: Optional[Sequence[RouteProvider]] = None
                           ) -> "tuple[Optional[RouteQuote], str]":
    """``(route, reason)``. ``reason`` is empty when a route was found.

    The reason exists because "no route" and "we could not ask" are different
    facts with different next moves, and the agent turns the first one into a
    written conclusion ("this token is unreachable"). A throttled or down
    provider must never produce that sentence — the honest report is that the
    lookup failed and is worth retrying.

    Selection is NOT best-priced across providers: local construction wins where
    it exists, because a marginally better number from a third party does not
    buy back the trust of building the call ourselves. The aggregator is a reach
    extension, not a price competition.
    """
    chain_providers = providers if providers is not None else _default_providers()
    unavailable: list = []
    asked: list = []

    for prov in chain_providers:
        name = getattr(prov, "name", prov.__class__.__name__)
        try:
            if not prov.supports(chain):
                continue
            asked.append(name)
            quote = prov.quote(chain, token_in, token_out, amount_in_raw,
                               holder=holder, slippage_bps=slippage_bps)
        except Exception as exc:
            logger.warning("route provider %s failed on %s (%s) — trying the next",
                           name, chain, exc)
            unavailable.append(name)
            continue
        if quote is None:
            continue

        if quote.amount_out_raw <= 0:
            logger.warning("route provider %s quoted zero output — refused", name)
            continue
        if not _calldata_ok(quote.calldata):
            logger.warning("route provider %s returned malformed calldata — refused", name)
            continue
        if quote.value_raw:
            # An ERC-20 -> ERC-20 swap moves no native value. A non-zero value
            # here is an outflow the intent does not declare.
            logger.error("route provider %s attached native value %s to an "
                         "ERC-20 swap — REFUSED", name, quote.value_raw)
            continue
        if not _addresses_ok(chain, quote):
            logger.error(
                "route provider %s named spender=%s target=%s on %s — not the "
                "pinned address for that chain, or not a valid address at all. "
                "REFUSED; nothing was built.",
                name, quote.spender, quote.to, chain)
            continue
        floor = _verified_floor(quote, slippage_bps)
        if floor is None:
            logger.warning(
                "route provider %s: its encoded minimum output does not clear "
                "the caller's %sbps slippage bound (or collapses to zero) — "
                "refused. Nothing was built.", name, slippage_bps)
            continue
        return replace(quote, amount_out_min_raw=floor), ""

    if unavailable:
        return None, (
            f"could not complete the route lookup on {chain}: "
            f"{', '.join(unavailable)} was UNAVAILABLE (an error or a rate "
            f"limit, not an answer). This is NOT 'no route exists' — the "
            f"lookup did not finish. Retry before concluding the token is "
            f"unreachable.")
    return None, no_route_reason(chain, asked=asked)


def best_route(chain: str, token_in: str, token_out: str, amount_in_raw: int,
               *, holder: str, slippage_bps: int,
               providers: Optional[Sequence[RouteProvider]] = None
               ) -> Optional[RouteQuote]:
    """The first provider that routes, or ``None``. Fails open — "no route" is
    unknown, never a zero-output trade. Use ``best_route_with_reason`` when the
    caller renders a refusal to a human or an agent."""
    route, _reason = best_route_with_reason(
        chain, token_in, token_out, amount_in_raw, holder=holder,
        slippage_bps=slippage_bps, providers=providers)
    return route


def no_route_reason(chain: str, *, asked: Optional[Sequence[str]] = None) -> str:
    """Why nothing routed, naming who was asked — a refusal that teaches.

    The old message said "no Uniswap V3 route ... the pair may have no pool on
    this chain", which was true and useless: the agent could not tell whether a
    lever existed. It re-filed a proposal for a bug already fixed for exactly
    this reason.
    """
    from core.wallet import chains
    names = list(asked) if asked is not None else [p.name for p in providers()]
    asked = ", ".join(names) or "(none)"
    row = chains.get(chain)
    tail = ""
    if row is not None and row.aggregator_spender and "lifi" not in names:
        tail = (" An aggregator IS available on this chain and would reach pools "
                "outside Uniswap V3 (Aerodrome, V2 forks) — it is off; set "
                "DEFI_ROUTE_AGGREGATOR=lifi to enable it.")
    return (f"no route on {chain} — asked: {asked}. The pair may have no pool "
            f"any of them can reach.{tail}")
