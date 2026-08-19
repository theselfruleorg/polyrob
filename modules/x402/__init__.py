"""x402 payment protocol module.

Uses fastapi-x402 for proper on-chain verification via Coinbase facilitator.

Re-exports are LAZY (PEP 562): the middleware drags fastapi in, and light
consumers (e.g. the tools/x402 registration gate importing
``modules.x402.invoicing``) must not pay that import at boot.
``from modules.x402 import X402PaymentMiddleware`` still works unchanged.
"""

_LAZY_EXPORTS = {
    'X402PaymentMiddleware': ('modules.x402.middleware', 'X402PaymentMiddleware'),
    'generate_user_id_from_wallet': ('modules.x402.x402_integration', 'generate_user_id_from_wallet'),
    'ensure_user_profile_for_payer': ('modules.x402.x402_integration', 'ensure_user_profile_for_payer'),
    'record_x402_payment': ('modules.x402.x402_integration', 'record_x402_payment'),
    'get_x402_config': ('modules.x402.x402_integration', 'get_x402_config'),
    'is_x402_properly_configured': ('modules.x402.x402_integration', 'is_x402_properly_configured'),
}

__all__ = list(_LAZY_EXPORTS)


def __getattr__(name):
    try:
        modname, attr = _LAZY_EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    import importlib
    return getattr(importlib.import_module(modname), attr)


def __dir__():
    return sorted(set(globals()) | set(_LAZY_EXPORTS))
