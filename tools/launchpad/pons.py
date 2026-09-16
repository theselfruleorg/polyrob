"""Pons V2 — the first launchpad provider (042).

What a launchpad is, mechanically: a create-and-seed in ONE transaction against
a protocol we did not write. It is not a deploy (the factory owns the CREATE2)
and it is not a swap (there is no route and no pool yet), which is why it gets
its own verb rather than being bolted onto either.

Everything that can be read live IS read live, per call:

* the pins (``verify_pins``) — a factory whose deployed code no longer hashes to
  the pinned value REFUSES. It does not warn and proceed;
* ``launchFee()`` — storage, owner-settable, and the factory demands EXACT
  equality of ``msg.value``. A hardcoded 0.0005 reverts the day it changes;
* ``previewLaunchEconomics()`` — committed as ``expectedEconomics`` rather than
  waived, so terms that move between the quote and the broadcast revert instead
  of silently applying;
* ``launchEnabled()`` / ``canLaunch()`` / ``approvedPairTokens()``.

⚠️ **The snipe tax opens at 99%.** ``currentSnipeTaxBps`` is
``snipeTaxStartBps >> ((elapsed * 14) / snipeTaxSeconds)`` with a live start of
9900 bps over 3 seconds, so a buy in the launch block loses almost everything.
The factory auto-exempts the deployer and the creator-fee recipient; ANY other
recipient must be named in ``snipeTaxExemptions`` or it is taxed. Both verbs
here read the live rate and refuse rather than quietly paying it.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from core.wallet import abi
from tools.launchpad import pons_abi as P

logger = logging.getLogger(__name__)

#: Above this, a buy is refused rather than paid. 100 bps of snipe tax on top of
#: the 1% curve fee is already a bad fill; 9900 is a total loss.
MAX_TOLERATED_SNIPE_BPS = 100


class PonsError(RuntimeError):
    """Pons cannot be used for this. The message always says why."""


@dataclass(frozen=True)
class LaunchTerms:
    """What the factory says the terms ARE, right now."""
    enabled: bool
    launch_fee_wei: int
    supply_raw: int
    curve_fee_bps: int
    phantom_quote: int
    graduation_threshold: int
    economics: str
    pair_decimals: int


@dataclass(frozen=True)
class CurveState:
    """A live curve, enough to price a trade exactly."""
    curve: str
    token: str
    quote_reserve: int
    token_reserve: int
    reserved_tokens: int
    fee_bps: int
    creator_tax_bps: int
    snipe_tax_bps: int
    graduated: bool


# ==========================================================================
# Reads
# ==========================================================================

def _call(rpc, to: str, data: str) -> str:
    return rpc("eth_call", [{"to": to, "data": data}, "latest"])


def _view(rpc, to: str, spec: Dict[str, Any], args: Optional[List] = None):
    data = abi.encode_call(spec["name"], spec["inputs"], args or [])
    raw = _call(rpc, to, data)
    if raw in (None, "0x", ""):
        raise PonsError(
            f"{spec['name']} on {to} returned nothing — refusing to guess a "
            f"value the contract did not give")
    values = abi.decode(spec["outputs"], raw)
    return values[0] if len(values) == 1 else values


def verify_pins(rpc) -> None:
    """The factory and router must be EXACTLY the code that was reviewed.

    A launchpad pin is the one address the owner's money is aimed at. If the
    code at it has changed, nothing downstream — not the ABI, not the fee, not
    the economics commitment — describes what will run.
    """
    from eth_utils import keccak
    for address, expected in P.CODE_HASHES.items():
        code = rpc("eth_getCode", [address, "latest"])
        if not isinstance(code, str) or len(code) <= 2:
            raise PonsError(
                f"{address} has NO code on {P.CHAIN} — the pinned Pons "
                f"deployment is not there. Refusing.")
        got = "0x" + keccak(bytes.fromhex(code[2:])).hex()
        if got != expected:
            raise PonsError(
                f"the code at {address} hashes to {got}, not the pinned "
                f"{expected}. The Pons deployment has changed since it was "
                f"reviewed — refusing to launch into un-reviewed code.")


def read_terms(rpc, *, pair_token: str = P.NATIVE_PAIR,
               launch_config_id: int = P.LAUNCH_CONFIG_ID) -> LaunchTerms:
    """The live launch terms. Never cached, never assumed."""
    enabled = bool(_view(rpc, P.FACTORY, P.LAUNCH_ENABLED))
    fee = int(_view(rpc, P.FACTORY, P.LAUNCH_FEE))
    config = _view(rpc, P.FACTORY, P.GET_LAUNCH_CONFIG, [int(launch_config_id)])
    supply, curve_fee_bps, phantom, threshold, _pool_fee, _tick, cfg_enabled = config
    if not cfg_enabled:
        raise PonsError(
            f"launch config {launch_config_id} is disabled on the factory")
    economics = _view(rpc, P.FACTORY, P.PREVIEW_ECONOMICS,
                      [int(launch_config_id), pair_token])

    decimals = 18
    if pair_token.lower() != P.NATIVE_PAIR.lower():
        if not bool(_view(rpc, P.FACTORY, P.APPROVED_PAIR, [pair_token])):
            raise PonsError(
                f"{pair_token} is not an approved Pons pair token. Approved "
                f"pairs are the chain's tokenized equities, USDG and cbBTC; "
                f"an arbitrary token cannot quote a launch.")
        phantom, threshold, decimals = _view(
            rpc, P.FACTORY, P.PAIR_TOKEN_ECONOMICS, [pair_token])
        # ⚠️ NOT always 18. USDG is 6 and cbBTC is 8; assuming 18 misprices the
        # quote leg by twelve orders of magnitude.
        decimals = int(decimals)

    return LaunchTerms(
        enabled=enabled, launch_fee_wei=fee, supply_raw=int(supply),
        curve_fee_bps=int(curve_fee_bps), phantom_quote=int(phantom),
        graduation_threshold=int(threshold), economics=economics,
        pair_decimals=decimals)


def launched_token(rpc, token: str) -> Optional[Dict[str, Any]]:
    """The factory's own record for *token*, or None when it did not launch it.

    This is how a curve is discovered, and it is also the membership test: a
    non-Pons address comes back with ``exists=False``. Never trust a curve
    address from anywhere else — a curve cannot be pinned by code hash (each
    one bakes its own ``creatorTaxBps`` and ``deployer`` into its runtime), so
    PROVENANCE is the only check available.
    """
    row = _view(rpc, P.FACTORY, P.GET_LAUNCHED_TOKEN, [token])
    keys = [c["name"] for c in P.GET_LAUNCHED_TOKEN["outputs"][0]["components"]]
    record = dict(zip(keys, row))
    return record if record.get("exists") else None


def curve_state(rpc, curve: str, *, recipient: str) -> CurveState:
    """Everything needed to price a trade on *curve*, measured now."""
    factory = _view(rpc, curve, P.CURVE_FACTORY)
    if str(factory).lower() != P.FACTORY.lower():
        raise PonsError(
            f"the curve at {curve} reports factory {factory}, not the pinned "
            f"Pons factory {P.FACTORY} — it is not a Pons curve")
    token = _view(rpc, curve, P.CURVE_TOKEN)
    quote_reserve, token_reserve = _view(rpc, curve, P.CURVE_GET_RESERVES)
    return CurveState(
        curve=curve, token=str(token),
        quote_reserve=int(quote_reserve), token_reserve=int(token_reserve),
        reserved_tokens=int(_view(rpc, curve, P.CURVE_RESERVED_TOKENS)),
        fee_bps=int(_view(rpc, curve, P.CURVE_FEE_BPS)),
        creator_tax_bps=int(_view(rpc, curve, P.CURVE_CREATOR_TAX_BPS)),
        snipe_tax_bps=int(_view(rpc, curve, P.CURVE_SNIPE_TAX_BPS, [recipient])),
        graduated=bool(_view(rpc, curve, P.CURVE_GRADUATED)))


@dataclass(frozen=True)
class Claimable:
    """What the launchpad is holding for us, and where."""
    escrow: str
    native_wei: int
    unswept_wei: Optional[int] = None


def fee_escrow(rpc, curve: str) -> str:
    """The escrow a curve credits creator tax to.

    Read from the CURVE, never supplied by a caller: that is what makes a claim
    unable to be aimed at an arbitrary contract. The curve itself is only
    reachable through the pinned factory's own launch record.
    """
    return str(_view(rpc, curve, P.CURVE_FEE_ESCROW))


def claimable(rpc, curve: str, *, holder: str) -> Claimable:
    """How much native the escrow owes *holder*, measured now.

    ⚠️ The balance lives on the ESCROW, not the curve. Production read the curve
    for it on 2026-09-14, got 0 and reverts from guessed function names, and
    reported the fees "unproven" while 13.613 ETH sat one hop away.

    `creatorTaxBalance()` on the curve is the portion not yet swept into the
    escrow — reported separately because it is real money that is not claimable
    yet, and folding the two into one figure would misstate both.
    """
    escrow = fee_escrow(rpc, curve)
    owed = int(_view(rpc, escrow, P.ESCROW_BALANCE_OF, [holder]))
    try:
        unswept = int(_view(rpc, curve, P.CURVE_CREATOR_TAX_BALANCE))
    except Exception as exc:
        # Supplementary. A curve generation that does not expose it must not
        # block a claim of money the escrow has already confirmed it owes.
        logger.debug("pons: creatorTaxBalance unreadable on %s (%s)", curve, exc)
        unswept = None
    return Claimable(escrow=escrow, native_wei=owed, unswept_wei=unswept)


def build_claim(*, escrow: str) -> Dict[str, Any]:
    """The claim call. No arguments: the escrow pays the caller what it owes the
    caller, so there is no amount and no recipient to get wrong."""
    return {"to": escrow,
            "calldata": abi.encode_call(P.ESCROW_CLAIM["name"],
                                        P.ESCROW_CLAIM["inputs"], []),
            "value_wei": 0}


# ==========================================================================
# Pricing — verified against six live simulations, delta 0
# ==========================================================================

def quote_buy(state: CurveState, quote_in: int) -> Tuple[int, Dict[str, int]]:
    """``(tokens_out, legs)`` for spending *quote_in* on the curve.

    ⚠️ Each fee leg floors SEPARATELY. Computing
    ``quote_in * (10000 - fee - tax) // 10000`` is off by one wei, which is
    enough to make a min-out assertion fire on a correct trade.
    """
    if quote_in <= 0:
        raise PonsError("quote_in must be positive")
    fee = quote_in * state.fee_bps // 10_000
    tax = quote_in * state.creator_tax_bps // 10_000
    snipe = quote_in * state.snipe_tax_bps // 10_000
    net = quote_in - fee - tax - snipe
    if net <= 0:
        raise PonsError(
            f"the fees on this curve ({state.fee_bps + state.creator_tax_bps + state.snipe_tax_bps} "
            f"bps) consume the whole input")
    tokens_out = state.token_reserve * net // (state.quote_reserve + net)
    sellable = max(0, state.token_reserve - state.reserved_tokens)
    clamped = tokens_out > sellable
    return (min(tokens_out, sellable) if clamped else tokens_out), {
        "fee": fee, "creator_tax": tax, "snipe_tax": snipe, "net": net,
        "clamped_at_graduation": int(clamped)}


def quote_sell(state: CurveState, tokens_in: int) -> Tuple[int, Dict[str, int]]:
    """``(quote_out, legs)`` for selling *tokens_in*. Fees come off the OUTPUT."""
    if tokens_in <= 0:
        raise PonsError("tokens_in must be positive")
    gross = state.quote_reserve * tokens_in // (state.token_reserve + tokens_in)
    fee = gross * state.fee_bps // 10_000
    tax = gross * state.creator_tax_bps // 10_000
    return gross - fee - tax, {"gross": gross, "fee": fee, "creator_tax": tax}


# ==========================================================================
# Calldata
# ==========================================================================

def _token_params(*, name: str, symbol: str, logo: str, description: str,
                  socials: Tuple[str, str, str, str, str],
                  creator: str, creator_tax_bps: int, buyback: bool,
                  economics: str, salt: str) -> list:
    if creator_tax_bps < 0 or creator_tax_bps > P.MAX_CREATOR_TAX_BPS:
        raise PonsError(
            f"creator_tax_bps must be 0..{P.MAX_CREATOR_TAX_BPS} "
            f"(the factory's maxCreatorTaxBps), got {creator_tax_bps}")
    return [name, symbol, logo, description, list(socials), creator,
            int(creator_tax_bps), bool(buyback), economics, salt]


def new_salt() -> str:
    """A fresh CREATE2 salt.

    The salt is namespaced per deployer (``keccak(abi.encode(deployer, salt))``),
    so it only has to be unique for THIS wallet — and reusing one on identical
    terms reverts. Random 32 bytes rather than a counter: a counter would have
    to be persisted, and a persisted counter that drifts reverts a launch for a
    reason nobody can see.
    """
    return "0x" + os.urandom(32).hex()


def build_launch(*, terms: LaunchTerms, name: str, symbol: str,
                 creator: str, logo: str = "", description: str = "",
                 socials: Tuple[str, str, str, str, str] = ("", "", "", "", ""),
                 creator_tax_bps: int = 100, buyback: bool = False,
                 pair_token: str = P.NATIVE_PAIR,
                 launch_config_id: int = P.LAUNCH_CONFIG_ID,
                 salt: Optional[str] = None,
                 snipe_exemptions: Tuple[str, ...] = ()) -> Dict[str, Any]:
    """A launch with NO opening buy: straight to the factory."""
    if len(snipe_exemptions) > P.MAX_SNIPE_EXEMPTIONS:
        raise PonsError(
            f"at most {P.MAX_SNIPE_EXEMPTIONS} snipe-tax exemptions, got "
            f"{len(snipe_exemptions)}")
    params = _token_params(
        name=name, symbol=symbol, logo=logo, description=description,
        socials=socials, creator=creator, creator_tax_bps=creator_tax_bps,
        buyback=buyback, economics=terms.economics, salt=salt or new_salt())
    calldata = abi.encode_call(
        P.LAUNCH_TOKEN["name"], P.LAUNCH_TOKEN["inputs"],
        [params, int(launch_config_id), pair_token, list(snipe_exemptions)])
    # ⚠️ EXACT equality: `if (msg.value != launchFee) revert LaunchFeeNotPaid()`.
    return {"to": P.FACTORY, "calldata": calldata,
            "value_wei": terms.launch_fee_wei, "salt": params[-1]}


def build_launch_and_buy(*, terms: LaunchTerms, name: str, symbol: str,
                         creator: str, recipient: str, quote_in: int,
                         min_tokens_out: int, logo: str = "",
                         description: str = "",
                         socials: Tuple[str, str, str, str, str] = ("", "", "", "", ""),
                         creator_tax_bps: int = 100, buyback: bool = False,
                         pair_token: str = P.NATIVE_PAIR,
                         launch_config_id: int = P.LAUNCH_CONFIG_ID,
                         salt: Optional[str] = None,
                         snipe_exemptions: Tuple[str, ...] = ()) -> Dict[str, Any]:
    """A launch WITH an opening buy, through the router.

    ⚠️ The router rejects ``quoteIn == 0`` — it always buys. And it calls
    ``launchTokenFor(..., msg.sender, ...)``, so the recorded deployer is this
    wallet, not the router, which is what makes the deployer auto-exemption
    apply to us.
    """
    if quote_in <= 0:
        raise PonsError(
            "the router always performs an opening buy and rejects quote_in=0 — "
            "use a plain launch when you do not want one")
    exemptions = list(snipe_exemptions)
    if recipient.lower() != creator.lower() and recipient.lower() not in {
            e.lower() for e in exemptions}:
        # The factory auto-exempts the deployer and the creator-fee recipient
        # and NOBODY else. An un-exempted opening buy pays the 99% opening rate.
        exemptions.append(recipient)
    if len(exemptions) > P.MAX_SNIPE_EXEMPTIONS:
        raise PonsError(
            f"at most {P.MAX_SNIPE_EXEMPTIONS} snipe-tax exemptions, got "
            f"{len(exemptions)}")
    params = _token_params(
        name=name, symbol=symbol, logo=logo, description=description,
        socials=socials, creator=creator, creator_tax_bps=creator_tax_bps,
        buyback=buyback, economics=terms.economics, salt=salt or new_salt())
    calldata = abi.encode_call(
        P.LAUNCH_AND_BUY["name"], P.LAUNCH_AND_BUY["inputs"],
        [params, int(launch_config_id), pair_token, int(quote_in),
         int(min_tokens_out), recipient, exemptions])
    native = pair_token.lower() == P.NATIVE_PAIR.lower()
    # `expectedValue = nativeQuote ? launchFee + quoteIn : launchFee`, then an
    # exact-equality check. An ERC-20 pair needs the ROUTER approved for quoteIn.
    value = terms.launch_fee_wei + (int(quote_in) if native else 0)
    return {"to": P.ROUTER, "calldata": calldata, "value_wei": value,
            "salt": params[-1], "needs_erc20_approval": not native}


def build_buy(*, curve: str, quote_in: int, min_tokens_out: int,
              recipient: str, native_quote: bool) -> Dict[str, Any]:
    calldata = abi.encode_call(P.CURVE_BUY["name"], P.CURVE_BUY["inputs"],
                               [int(quote_in), int(min_tokens_out), recipient])
    return {"to": curve, "calldata": calldata,
            "value_wei": int(quote_in) if native_quote else 0}


def build_sell(*, curve: str, tokens_in: int, min_quote_out: int,
               recipient: str) -> Dict[str, Any]:
    """A sell needs NO approval — a Pons token grants its own curve max allowance."""
    calldata = abi.encode_call(P.CURVE_SELL["name"], P.CURVE_SELL["inputs"],
                               [int(tokens_in), int(min_quote_out), recipient])
    return {"to": curve, "calldata": calldata, "value_wei": 0}


def parse_token_launched(logs) -> Optional[Dict[str, str]]:
    """``{token, curve, deployer}`` from a receipt, or None.

    The address is read from the RECEIPT, never predicted: Pons deploys through
    CREATE2 inside its own factory, so the nonce-based prediction the deploy
    verb uses does not apply here and guessing would name the wrong contract.
    """
    from eth_utils import to_checksum_address
    for entry in logs or []:
        topics = entry.get("topics") or []
        if not topics or str(topics[0]).lower() != P.TOPIC_TOKEN_LAUNCHED:
            continue
        if len(topics) < 4:
            continue
        return {
            "token": to_checksum_address("0x" + str(topics[1])[-40:]),
            "curve": to_checksum_address("0x" + str(topics[2])[-40:]),
            "deployer": to_checksum_address("0x" + str(topics[3])[-40:]),
        }
    return None
