"""x402 payment protocol module.

Uses fastapi-x402 for proper on-chain verification via Coinbase facilitator.
"""

from .middleware import X402PaymentMiddleware
from .x402_integration import (
    generate_user_id_from_wallet,
    ensure_user_profile_for_payer,
    record_x402_payment,
    get_x402_config,
    is_x402_properly_configured,
)

__all__ = [
    'X402PaymentMiddleware',
    'generate_user_id_from_wallet',
    'ensure_user_profile_for_payer',
    'record_x402_payment',
    'get_x402_config',
    'is_x402_properly_configured',
]
