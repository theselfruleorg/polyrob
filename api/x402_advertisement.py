"""What the public x402 surfaces may CLAIM about this instance.

Three facts that were hardcoded in two places each and wrong in both:

* **B22** — ``supported_assets: ["usdc", "usdt", "eth"]``. The per-request x402
  rail settles through ``fastapi_x402``, which knows exactly one asset: USDC.
  A payer who sent USDT or ETH on the strength of that list paid an address
  nothing would ever match to their request.
* **B23** — ``credits.enabled: True``. Credits exist only when the account
  system is on AND the credit system is on (``core/initialization.py`` registers
  ``balance_manager`` under both). Advertised unconditionally, the card told a
  caller to buy credits on an instance with no ledger.
* **B41** — ``resolve_treasury_address()`` reaches into the agent wallet and
  DERIVES a signing key to read its address. That ran once per unauthenticated
  request to ``/.well-known/agent.json`` and ``/api/x402/pricing``. The address
  of a wallet does not change while the process lives, so it is resolved once
  and cached.
"""

import logging
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: The per-request x402 rail — the one ``modules/x402/middleware.py`` settles on.
FACILITATOR_RAIL = "facilitator"

_treasury_lock = threading.Lock()
_treasury_cache: Optional[str] = None
_treasury_resolved = False


def treasury_address(*, refresh: bool = False) -> str:
    """The advertised pay-to address, resolved ONCE per process (B41).

    Delegates to the ONE resolver (``modules.x402.x402_integration
    .resolve_treasury_address``); the legacy ``X402_PAYMENT_ADDRESS`` env
    spelling stays as a last fallback. An EMPTY result is not cached — an
    address that could not be read yet (wallet still initializing) must be
    retried, or the instance advertises "no treasury" for its whole lifetime.
    """
    global _treasury_cache, _treasury_resolved
    if refresh:
        with _treasury_lock:
            _treasury_cache, _treasury_resolved = None, False
    if _treasury_resolved and _treasury_cache:
        return _treasury_cache
    with _treasury_lock:
        if _treasury_resolved and _treasury_cache:
            return _treasury_cache
        import os

        from modules.x402.x402_integration import resolve_treasury_address

        address = resolve_treasury_address() or os.environ.get(
            "X402_PAYMENT_ADDRESS", "")
        address = (address or "").strip()
        if address:
            _treasury_cache = address
            _treasury_resolved = True
        return address


def reset_treasury_cache() -> None:
    """Drop the cached address (tests, and any future key rotation)."""
    global _treasury_cache, _treasury_resolved
    with _treasury_lock:
        _treasury_cache, _treasury_resolved = None, False


def _facilitator_assets() -> List[Any]:
    """Builtin asset rows whose rail is the per-request x402 facilitator."""
    try:
        from core.payments.assets import BUILTIN_ASSETS

        return [a for a in BUILTIN_ASSETS.values() if a.rail == FACILITATOR_RAIL]
    except Exception as e:
        logger.debug("x402 advertisement: asset registry unreadable (%s)", e)
        return []


def supported_assets() -> List[str]:
    """Asset symbols the per-request x402 rail can actually settle (B22).

    Derived from ``core.payments.assets`` rather than written out, so an asset
    added to the registry appears here and one that was never supported cannot
    linger in a hand-kept list. Empty when the registry is unreadable — an
    empty list here means "ask /api/x402/pricing", never "any asset works".
    """
    return sorted({a.symbol.lower() for a in _facilitator_assets()})


def supported_chains() -> List[str]:
    """Chains the per-request x402 rail can settle on (B22).

    Narrowed to the network this instance is actually CONFIGURED for
    (``X402_NETWORK``). The asset registry carries both ``base`` and
    ``base-sepolia``; advertising both told a payer of a mainnet instance that
    testnet USDC would settle — worthless tokens sent to a real treasury, and
    the exact confidently-wrong shape B22 was raised about. When the configured
    network is not one the registry knows, every rail chain is listed rather
    than an empty list (an empty list would read as "no chain works").
    """
    chains = sorted({a.chain for a in _facilitator_assets()})
    try:
        # `x402_network()` and NOT `get_x402_config()`: the latter resolves the
        # treasury, which derives a wallet signing key — the per-request cost
        # B41 removed from exactly these two public endpoints.
        from modules.x402.x402_integration import x402_network

        network = x402_network().lower()
    except Exception as e:
        logger.debug("x402 advertisement: network unreadable (%s)", e)
        return chains
    return [network] if network in chains else chains


def credits_enabled() -> bool:
    """Whether this instance actually has a credit ledger (B23).

    Observable, not declared: ``balance_manager`` is registered only when
    ``ENABLE_AUTH`` and the credit system are both on.
    """
    try:
        from core.container import DependencyContainer

        container = DependencyContainer.get_instance()
        return bool(container and container.get_service("balance_manager"))
    except Exception:
        return False


def credits_block() -> Dict[str, Any]:
    """The ``pricing.credits`` block, honest about whether credits exist."""
    enabled = credits_enabled()
    if not enabled:
        return {
            "enabled": False,
            "description": ("Credits are not enabled on this instance — pay "
                            "per request with x402, or use an API key."),
        }
    from modules.credits.pricing import pricing

    markup = pricing.get_markup_info()
    return {
        "enabled": True,
        "credit_cost_usd": markup["credit_value_usd"],
        "session_credits": pricing.SESSION_CREATION_COST,
        "description": "For registered users with pre-purchased credits",
    }
