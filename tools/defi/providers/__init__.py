"""Third-party data providers for the DeFi read tier.

Everything these return is a CLAIM (see base.py). Data fails open; screening
fails closed.
"""
from tools.defi.providers.base import (  # noqa: F401
    Candidate, PriceInfo, Provider, ScreenVerdict,
    LIQUIDITY_CONFIDENCE_FLOOR_USD, confidence_for,
)
