"""The money rail's API contributions, still in tree (067 P5a).

Everything here is registered into ``api.contributions`` — the seam
``api/app.py``, ``api/dependencies.py``, ``api/payment_verification.py`` and
the A2A agent card read. The code of each piece moved here UNCHANGED from the
call site that used to hold it inline. 067 P5c moves this module into the
wallet pack (its ``pack()`` makes the same registrations); a server without it
has no payment/x402/ERC-8004 routes, no x402 middleware, no x402 auth path and
no x402 option in a 402 body or the agent card.

Importing this module imports no rail code: every reference is resolved when
its slot is used.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from api.contributions import (register_auth_method, register_card_payment_option,
                               register_middleware, register_payment_option,
                               register_router)

_SOURCE = "api.money_contributions"


# --- middleware -------------------------------------------------------------- #

def install_x402_middleware(app, logger) -> None:
    """Add x402 payment middleware (runs BEFORE JWT middleware due to LIFO order).
    This allows x402 payments to bypass JWT auth for pay-per-request access.
    Uses fastapi-x402 library for proper on-chain verification via Coinbase facilitator."""
    x402_enabled = os.environ.get("X402_ENABLED", "false").lower() == "true"
    if x402_enabled:
        try:
            from modules.x402.middleware import X402PaymentMiddleware, install_auth_state_writer
            from api.auth_state import set_auth_state

            # R-4 inversion: modules/x402 no longer imports api.auth_state; the
            # api tier installs the canonical C4 writer at mount time.
            install_auth_state_writer(set_auth_state)
            app.add_middleware(X402PaymentMiddleware, enabled=True)
            logger.info("✅ x402 payment middleware enabled (using fastapi-x402)")
        except ImportError as e:
            logger.warning(f"Could not add x402 middleware: {e}")


# --- router announcements (the log lines the inline mounts printed) ---------- #

def announce_payments(logger) -> None:
    logger.info("✅ Payment endpoints registered at /api/payments")


def announce_x402(logger) -> None:
    x402_enabled = os.environ.get("X402_ENABLED", "false").lower() == "true"
    logger.info(f"✅ x402 endpoints registered at /api/x402 (payments "
                f"{'enabled' if x402_enabled else 'info-only'})")


def announce_eip8004(logger) -> None:
    eip8004_enabled = os.environ.get("EIP8004_ENABLED", "false").lower() == "true"
    logger.info("✅ ERC-8004 Trustless Agents endpoints registered at /eip8004")
    logger.info("   - Registration: /eip8004/registration.json")
    logger.info("   - Reputation: /eip8004/reputation/*")
    logger.info("   - Validation: /eip8004/validation/*")
    logger.info(f"   - Status: {'enabled' if eip8004_enabled else 'discovery-only'}")


# --- the x402 auth path (get_user_permissive, path 1) ------------------------ #

def x402_user(request) -> Optional[str]:
    """x402 payment (``request.state.payment_method == "x402"``):
    a. ``request.state.user_id`` if the middleware set it;
    b. derived from ``request.state.payer_address``;
    c. the literal ``"x402_user"``. Not an x402 request -> None."""
    if not (hasattr(request.state, "payment_method")
            and request.state.payment_method == "x402"):
        return None
    user_id = getattr(request.state, "user_id", None)
    if user_id:
        return user_id
    # Fallback to wallet-derived ID if middleware didn't set it
    from core.identity import generate_user_id_from_wallet

    payer_address = getattr(request.state, "payer_address", None)
    if payer_address:
        return generate_user_id_from_wallet(payer_address)
    return "x402_user"


# --- the x402 option of a 402 body ------------------------------------------- #

def x402_payment_option(request, cost_credits: int = 1) -> Dict[str, Any]:
    """``payment_options["x402"]`` of ``payment_required_response``."""
    from modules.x402.x402_integration import get_x402_price_usd
    from modules.x402.middleware import build_x402_challenge

    # C2: single price SSOT — the quoted x402 price MUST equal the live charge.
    x402_cost_usd = get_x402_price_usd()

    # G-16: share the SAME challenge builder the middleware's own early 402 uses
    # (`build_x402_challenge`) instead of reading `app.state.x402_handler` — that
    # attribute is never assigned anywhere, so that branch always produced an
    # EMPTY payment_details dict. A payer hitting either 402 producer now gets
    # the same usable `accepts` block.
    x402_challenge = build_x402_challenge(request.url.path, cost_usd=x402_cost_usd)
    payment_details = x402_challenge["accepts"][0]
    return {
        "cost_usd": x402_cost_usd,
        "payment_details": payment_details,
        "accepts": x402_challenge["accepts"],
        "x402Version": x402_challenge["x402Version"],
    }


# --- the A2A agent card's x402 option (moved from api/a2a/agent_card.py) ---- #

def _x402_price_usd() -> float:
    """Single x402 price source (F12) — keeps the card aligned with the live charge."""
    from modules.x402.x402_integration import get_x402_price_usd
    return get_x402_price_usd()


def _resolve_payment_address() -> str:
    """Single treasury source (W2.2): the same resolver invoices/challenges
    use (env wins, agent wallet fills an empty env — W1.1), with the legacy
    `X402_PAYMENT_ADDRESS` env spelling kept as a last fallback.

    B41: routed through ``api.x402_advertisement.treasury_address``, which
    caches the answer per process — this card is PUBLIC and unauthenticated,
    and the underlying resolver derives a wallet signing key on every call."""
    from api.x402_advertisement import treasury_address
    return treasury_address()


def _supported_assets() -> List[str]:
    """Assets the per-request x402 rail can actually settle (B22)."""
    from api.x402_advertisement import supported_assets
    return supported_assets()


def _supported_chains() -> List[str]:
    """Chains the per-request x402 rail can actually settle on (B22)."""
    from api.x402_advertisement import supported_chains
    return supported_chains()


def x402_card_option() -> Optional[Dict[str, Any]]:
    """The agent card's ``authentication_options.x402`` block (the ``a2a.payment``
    hook). Raises ``ImportError`` when the x402 rail cannot import; the card
    then omits it (067 P0.9)."""
    return {
        "description": "Pay-per-request with crypto. No account needed.",
        "how_to_use": (
            "Standard x402 flow: 1) send your request; 2) on HTTP 402 read the "
            "payment requirements; 3) retry with an X-PAYMENT header (base64 "
            "EIP-3009 authorization). Settlement is handled automatically."
        ),
        "per_request_usd": _x402_price_usd(),
        # B22: derived from the asset registry, not a hand-kept list.
        # The old ["usdc","usdt","eth"] / ["base","ethereum"] pair was
        # false in both halves — the per-request rail settles USDC on
        # Base through fastapi_x402 and nothing else, so a payer who
        # believed the card and sent USDT or ETH paid an address that
        # would never be matched to their request.
        "supported_chains": _supported_chains(),
        "supported_assets": _supported_assets(),
        # W2.2 (2026-08-21): same resolver invoices use (env wins,
        # wallet fills in) — the card and invoices can never disagree.
        "payment_address": _resolve_payment_address(),
        "facilitator": os.environ.get("X402_FACILITATOR_URL", "") or "Direct signature verification"
    }


register_middleware("payment", f"{__name__}:install_x402_middleware", source=_SOURCE)
register_router("payments", "api.payment_endpoints:router", prefix="/api",
                tags=("payments",), source=_SOURCE, announce=f"{__name__}:announce_payments")
# x402 router: always registered — its endpoints self-check if x402 is configured.
register_router("x402", "api.x402_endpoints:router", prefix="/api", tags=("x402",),
                source=_SOURCE, announce=f"{__name__}:announce_x402",
                missing_level=logging.DEBUG)  # as before: a debug line
register_router("eip8004", "api.eip8004_endpoints:router",
                tags=("eip8004-trustless-agents",), source=_SOURCE,
                announce=f"{__name__}:announce_eip8004")
register_auth_method("x402", f"{__name__}:x402_user", source=_SOURCE)
register_payment_option("x402", f"{__name__}:x402_payment_option", source=_SOURCE)
register_card_payment_option("x402", f"{__name__}:x402_card_option", source=_SOURCE)
