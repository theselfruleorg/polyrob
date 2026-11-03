"""Authentication modules: API keys, tiers, identity mapping and wallet (SIWE) sign-in.

``SIWEAuthenticator`` needs the ``crypto`` extra (eth_account, web3), so it is
imported on first use only. API keys and tiers work on a base install.
"""

from .identity_mapper import IdentityMapper
from .tier_manager import TierManager
from .api_key_manager import APIKeyManager

__all__ = [
    'IdentityMapper',
    'TierManager',
    'APIKeyManager',
    'SIWEAuthenticator'
]


def __getattr__(name):
    if name == 'SIWEAuthenticator':
        from .siwe_auth import SIWEAuthenticator
        return SIWEAuthenticator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
