"""Jupiter — Solana's route aggregator. Phase 3.

Settles hard mismatch #2 of the Solana research ("accept an aggregator-authored
transaction, or integrate one AMM directly") by PRECEDENT rather than argument.
LI.FI's aggregator-authored calldata was accepted on EVM under simulation +
delta assertion + caps, and a live production trade showed the discipline holds:
the guard refused what it should and no allowance was left standing. Jupiter is
the same bargain on a different chain — it decides the PATH, and what we accept
as an OUTCOME stays ours.

Two Solana-specific differences from the LI.FI provider, both structural:

* **It returns a whole transaction, not calldata.** So there is nothing to
  build locally and no `to`/`spender` split to pin. What replaces the spender
  check is the FEE PAYER check in `SolanaSigner.sign_transaction`: a transaction
  that does not pay from our account is not ours to sign.
* **There is no allowance to grant or revoke** (mismatch #3). Jupiter needs no
  standing delegate, so `approve -> swap -> revoke` has no meaning here. The
  Solana verb set is deliberately smaller, and `solana_simulation`'s authority
  taxonomy — not an allowance check — is what catches a hidden grant.

Trust semantics match `providers/base.py`: a quote fails OPEN to ``None``, so
"no route" is unknown and never a zero-output trade.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"
SWAP_URL = "https://lite-api.jup.ag/swap/v1/swap"
TIMEOUT_SEC = 12.0

name = "jupiter"

#: Same 1bp rounding grace the EVM seam uses, and for the same measured reason:
#: an aggregator's own minimum lands within rounding of ours, and a strict
#: comparison would refuse good routes.
_FLOOR_GRACE_BPS = 1


@dataclass(frozen=True)
class JupiterQuote:
    chain: str
    token_in: str
    token_out: str
    amount_in_raw: int
    amount_out_raw: int
    amount_out_min_raw: int
    venue: str
    raw: Dict[str, Any]


def supports(chain: str) -> bool:
    """Solana only. A provider asked about another chain must DECLINE rather
    than answer for the one it knows."""
    from core.wallet import chains
    row = chains.get(chain)
    return bool(row and row.family == "svm")


def _int(value, default=None):
    from tools.defi.providers._http import parse_int
    return parse_int(value, default)


def parse_quote(payload: Any, *, chain: str) -> Optional[JupiterQuote]:
    """Pure. ``None`` = no usable route (never a zero-output trade)."""
    if not isinstance(payload, dict):
        return None
    out = _int(payload.get("outAmount"))
    floor = _int(payload.get("otherAmountThreshold"))
    if not out or out <= 0:
        return None
    if floor is None or floor <= 0:
        # An unverifiable floor is not a floor — same rule as the EVM seam.
        return None
    labels = []
    for hop in (payload.get("routePlan") or []):
        label = ((hop or {}).get("swapInfo") or {}).get("label")
        if label:
            labels.append(str(label))
    venue = "jupiter:" + ("+".join(labels[:3]) if labels else "unknown")
    return JupiterQuote(
        chain=chain,
        token_in=str(payload.get("inputMint") or ""),
        token_out=str(payload.get("outputMint") or ""),
        amount_in_raw=_int(payload.get("inAmount"), 0) or 0,
        amount_out_raw=out,
        amount_out_min_raw=floor,
        venue=venue,
        raw=payload)


def verified_floor(payload: Any, *, slippage_bps: int) -> Optional[int]:
    """Jupiter's own minimum, or ``None`` to refuse.

    Identical asymmetry to the EVM seam: Jupiter bakes its slippage into the
    transaction it builds, so our bound is a bar its floor must CLEAR, not a
    number we can impose.
    """
    q = parse_quote(payload, chain="solana")
    if q is None:
        return None
    ours = (q.amount_out_raw * (10_000 - slippage_bps)) // 10_000
    if ours <= 0:
        return None
    grace = (q.amount_out_raw * _FLOOR_GRACE_BPS) // 10_000
    if q.amount_out_min_raw < ours - grace:
        return None
    return q.amount_out_min_raw


def decode_swap_transaction(body: Any) -> Optional[bytes]:
    """The base64 transaction Jupiter built, or ``None``."""
    if not isinstance(body, dict):
        return None
    encoded = body.get("swapTransaction")
    if not encoded or not isinstance(encoded, str):
        return None
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        logger.warning("jupiter: swapTransaction was not valid base64 — refused")
        return None


# -- network boundary --------------------------------------------------------

def _get(url: str) -> Any:
    from tools.defi.providers._http import get_json
    return get_json(url, timeout=TIMEOUT_SEC)


def quote(token_in: str, token_out: str, amount_in_raw: int, *,
          slippage_bps: int, fetch=None) -> Optional[JupiterQuote]:
    params = {"inputMint": token_in, "outputMint": token_out,
              "amount": str(int(amount_in_raw)), "slippageBps": str(int(slippage_bps))}
    try:
        payload = (fetch or _get)(f"{QUOTE_URL}?{urllib.parse.urlencode(params)}")
    except Exception as exc:
        # Fails OPEN: an outage is unknown, not "this token is unreachable".
        logger.info("jupiter: no quote for %s->%s (%s)", token_in, token_out, exc)
        return None
    return parse_quote(payload, chain="solana")


def build_swap(quote_payload: Dict[str, Any], user_public_key: str,
               *, post=None) -> Optional[bytes]:
    """Ask Jupiter to build the transaction for an accepted quote."""
    body = json.dumps({"quoteResponse": quote_payload,
                       "userPublicKey": user_public_key,
                       "wrapAndUnwrapSol": True}).encode()
    if post is not None:
        return decode_swap_transaction(post(body))
    req = urllib.request.Request(SWAP_URL, data=body, headers={
        "content-type": "application/json", "user-agent": "polyrob-defi/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
            return decode_swap_transaction(json.loads(r.read()))
    except Exception as exc:
        logger.info("jupiter: swap build failed (%s)", exc)
        return None
