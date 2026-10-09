"""Resolve an expired x402 authorization from chain state (no operator needed).

An x402 payment journals an ``attempt:`` row BEFORE the SDK signs the EIP-3009
authorization. When the server answers a second 402, a ``success:false`` or
drops the connection, nothing proves the authorization was unused, so the row
stays unresolved — and an unresolved row refuses EVERY money rail. A hostile
paid server could hold the wallet that way until the owner released it.

The authorization itself bounds the doubt: after ``validBefore`` the token
refuses it, and ``authorizationState(authorizer, nonce)`` on the token says
whether it was ever used. So, once ``validBefore`` has passed:

* used (at the latest block)                       -> book the recorded amount
  (the signed ceiling) and release the row;
* unused at a FINALIZED block whose timestamp is
  past ``validBefore``                             -> it can never be used; book
  $0 (``never_sent``) and release the row, freeing the request's replay key;
* anything else (not yet expired, finality behind ``validBefore``, a wrong
  chain id, a malformed answer, an RPC failure)    -> keep the row (fail closed).

Rows written without the terms (legacy, or the bind failed) stay operator-only.
Read-only on chain: never signs or broadcasts.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

#: ``authorizationState(address,bytes32)`` — FiatToken v2 (USDC).
AUTHORIZATION_STATE_SELECTOR = "0xe94a0102"
#: Seconds past ``validBefore`` before the first look (clock skew margin).
EXPIRY_GRACE_SEC = 30
#: The autonomy runtime's expiry-watch interval.
INTERVAL_SEC = 120


def _networks():
    """network id (as the SDK/challenge names it) -> (rpc chain, chain id, USDC)."""
    from core.wallet.onchain import USDC_BASE_MAINNET, USDC_BASE_SEPOLIA
    main = ("base", 8453, USDC_BASE_MAINNET.lower())
    test = ("base-sepolia", 84532, USDC_BASE_SEPOLIA.lower())
    return {"base": main, "eip155:8453": main, "mainnet": main,
            "base-sepolia": test, "eip155:84532": test, "testnet": test}


@dataclass(frozen=True)
class ExpiryOutcome:
    reference: str
    outcome: str          # used | expired_unused | pending | unverifiable | released_elsewhere
    detail: str
    booked_usd: Optional[float] = None


def _word(value) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
        raise ValueError("invalid authorizationState answer")
    word = int(value, 16)
    if word not in (0, 1):
        raise ValueError("authorizationState answer is not a bool")
    return word


def _quantity(value) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{1,64}", value):
        raise ValueError("invalid RPC quantity")
    return int(value, 16)


def _default_call(chain: str, method: str, params: list):
    from core.wallet.onchain import _rpc, rpc_url_for_chain
    url = rpc_url_for_chain(chain)
    if not url:
        raise ValueError("no RPC endpoint for the authorization's chain")
    return _rpc(url, method, params, timeout=5)


def authorization_evidence(terms: dict, *, call: Callable, now: float) -> tuple[str, str]:
    """(outcome, detail) for one authorization. Raises on any doubt."""
    network = _networks().get(str(terms.get("network") or "").lower())
    if network is None:
        raise ValueError("authorization network is not a supported x402 network")
    chain, chain_id, usdc = network
    if str(terms.get("asset") or "").lower() != usdc:
        raise ValueError("authorization asset is not the canonical USDC")
    authorizer = str(terms["authorizer"]).lower()
    nonce = str(terms["nonce"]).lower()
    if not re.fullmatch(r"0x[0-9a-f]{40}", authorizer) or not re.fullmatch(r"0x[0-9a-f]{64}", nonce):
        raise ValueError("invalid stored authorization terms")
    valid_before = int(terms["valid_before"])
    if now <= valid_before + EXPIRY_GRACE_SEC:
        return "pending", f"authorization valid until {valid_before}; not yet expired"
    if _quantity(call(chain, "eth_chainId", [])) != chain_id:
        raise ValueError("RPC chain differs from the authorization's chain")
    data = AUTHORIZATION_STATE_SELECTOR + authorizer[2:].rjust(64, "0") + nonce[2:]
    request = {"to": usdc, "data": data}
    if _word(call(chain, "eth_call", [request, "latest"])):
        return "used", "authorizationState=used on chain"
    finalized = call(chain, "eth_getBlockByNumber", ["finalized", False])
    if not isinstance(finalized, dict):
        raise ValueError("no finalized block")
    number = _quantity(finalized.get("number"))
    if _quantity(finalized.get("timestamp")) <= valid_before:
        return "pending", "finality has not passed validBefore yet"
    if _word(call(chain, "eth_call", [request, hex(number)])):
        return "used", f"authorizationState=used at finalized block {number}"
    return "expired_unused", (f"authorizationState=unused at finalized block {number}, "
                              f"after validBefore {valid_before}")


def resolve_expired(*, call: Optional[Callable] = None, now: Optional[float] = None,
                    data_dir: Optional[str] = None) -> list[ExpiryOutcome]:
    """Resolve every expired x402 row the chain can decide. Never raises for a
    single row (that row stays unresolved); raises only if the journal itself
    is unreadable, so a caller never mistakes damage for "nothing to do"."""
    from core.wallet.submission_journal import x402_authorizations
    from core.wallet.submission_release import ReleaseRefused, release_submission
    call = call or _default_call
    now = time.time() if now is None else float(now)
    results = []
    for row in x402_authorizations(data_dir):
        ref = row["tx_hash"]
        try:
            outcome, detail = authorization_evidence(row["x402_auth"], call=call, now=now)
        except Exception as exc:  # noqa: BLE001 — never relay RPC text (URLs hold keys)
            results.append(ExpiryOutcome(ref, "unverifiable",
                                         f"could not read the authorization state "
                                         f"({type(exc).__name__}); row kept"))
            continue
        if outcome == "pending":
            results.append(ExpiryOutcome(ref, outcome, detail))
            continue
        evidence = {"outcome": f"x402_authorization_{outcome}", "detail": detail}
        try:
            entry = release_submission(
                ref, data_dir=data_dir, inspect=lambda _row: evidence,
                never_sent=(outcome == "expired_unused"),
                reason=f"auto: {detail}" if outcome == "expired_unused" else "",
                release_replay_key=(outcome == "expired_unused"))
        except ReleaseRefused as exc:
            results.append(ExpiryOutcome(ref, "released_elsewhere"
                                         if "no unresolved submission" in str(exc) else "unverifiable",
                                         str(exc)[:300]))
            continue
        except Exception as exc:  # noqa: BLE001
            results.append(ExpiryOutcome(ref, "unverifiable",
                                         f"release failed ({type(exc).__name__}); row kept"))
            continue
        logger.warning("x402 submission %s auto-resolved: %s", ref, detail)
        results.append(ExpiryOutcome(ref, outcome, detail, float(entry.get("amount_usd") or 0.0)))
    return results
