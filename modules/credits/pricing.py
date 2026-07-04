"""
Credits Module - Pricing Configuration

SINGLE SOURCE OF TRUTH for all credit pricing and cost calculation.

This config is used by:
- modules/credits/usage_tracker.py (calculates and charges)
- modules/credits/usage_meter.py (metering)
- modules/credits/balance_manager.py (balance operations)
- modules/auth/identity_mapper.py (welcome bonus, DEN allowance)
- modules/auth/tier_manager.py (tier limits)
- api/payment_endpoints.py (public pricing API)
- webview/stats_service.py (display fallback only)

Location: modules/credits/pricing.py
Why here? Pricing is OWNED by the credits module - it's business logic, not bot config.
"""

import math
import os
from typing import Dict


# ============================================================================
# CREDIT BONUSES - Single source of truth for all bonus amounts
# ============================================================================

# Welcome bonus given to ALL new users on sign up
WELCOME_BONUS: int = int(os.environ.get("WELCOME_BONUS", "100"))

# One-time DEN Sign Up Allowance per token ID (prevents transfer abuse)
DEN_SIGNUP_ALLOWANCE: int = int(os.environ.get("DEN_SIGNUP_ALLOWANCE", "2000"))


class PricingConfig:
    """Centralized pricing configuration for the entire application."""

    # ============================================================================
    # MARKUP CONFIGURATION (Core business logic)
    # ============================================================================

    # MARKUP: Users pay API cost × this multiplier
    # Examples:
    #   1.00 = 0% markup (users pay exactly what API costs)
    #   1.20 = 20% markup (users pay API cost + 20%)
    #   1.50 = 50% markup
    MARKUP: float = float(os.environ.get("PRICING_MARKUP", "1.00"))

    # ============================================================================
    # CREDIT SYSTEM (Currency conversion)
    # ============================================================================

    # How much USD one credit is worth
    CREDIT_VALUE_USD: float = float(os.environ.get("CREDIT_VALUE_USD", "0.01"))  # 1 credit = $0.01

    # Minimum charge per LLM call (prevents free calls)
    MIN_CREDIT_CHARGE: int = int(os.environ.get("MIN_CREDIT_CHARGE", "1"))  # Minimum 1 credit

    # ============================================================================
    # FIXED COSTS (Non-LLM resources)
    # ============================================================================

    # Session creation cost (infrastructure setup)
    SESSION_CREATION_COST: int = int(os.environ.get("SESSION_CREATION_COST", "1"))

    # Browser action costs
    BROWSER_PAGE_LOAD_COST: float = float(os.environ.get("BROWSER_PAGE_LOAD_COST", "0.5"))
    BROWSER_SCREENSHOT_COST: float = float(os.environ.get("BROWSER_SCREENSHOT_COST", "0.2"))
    BROWSER_INTERACTION_COST: float = float(os.environ.get("BROWSER_INTERACTION_COST", "0.1"))
    BROWSER_DEFAULT_COST: float = float(os.environ.get("BROWSER_DEFAULT_COST", "0.5"))

    # Special action costs
    VISION_CALL_COST: int = int(os.environ.get("VISION_CALL_COST", "2"))
    TOOL_CALL_COST: float = float(os.environ.get("TOOL_CALL_COST", "0.5"))

    @classmethod
    def get_markup_info(cls) -> Dict[str, any]:
        """Get markup information for display/reporting.

        Returns:
            Dict with markup details and formatted description
        """
        markup_percentage = (cls.MARKUP - 1.0) * 100

        return {
            "markup": cls.MARKUP,
            "markup_percentage": markup_percentage,
            "description": f"{markup_percentage:.0f}% markup" if markup_percentage > 0 else "No markup (API cost only)",
            "credit_value_usd": cls.CREDIT_VALUE_USD,
            "min_charge": cls.MIN_CREDIT_CHARGE
        }

    @classmethod
    def calculate_credits_from_api_cost(cls, api_cost_usd: float) -> tuple[int, float]:
        """Calculate credits to charge user from API cost.

        This is the CORE pricing logic used throughout the application.

        Args:
            api_cost_usd: Cost charged by API provider in USD

        Returns:
            Tuple of (credits_to_charge, user_cost_usd)
        """
        # Convert to credits with markup
        credits_raw = (api_cost_usd / cls.CREDIT_VALUE_USD) * cls.MARKUP

        # ALWAYS round UP to ensure we never charge less than API cost
        # Using math.ceil() instead of int(x + 0.5) which was incorrectly rounding DOWN
        # for values like 2.42 → int(2.92) = 2, losing money!
        credits_charged = max(cls.MIN_CREDIT_CHARGE, math.ceil(credits_raw))

        # Calculate what user actually pays
        user_cost_usd = credits_charged * cls.CREDIT_VALUE_USD

        return credits_charged, user_cost_usd

    @classmethod
    def get_browser_action_cost(cls, action_type: str) -> float:
        """Get cost for browser action.

        Args:
            action_type: Type of browser action (page_load, screenshot, interaction, etc.)

        Returns:
            Cost in credits
        """
        costs = {
            "page_load": cls.BROWSER_PAGE_LOAD_COST,
            "screenshot": cls.BROWSER_SCREENSHOT_COST,
            "interaction": cls.BROWSER_INTERACTION_COST,
        }
        return costs.get(action_type, cls.BROWSER_DEFAULT_COST)


# Create singleton instance for easy importing
# Usage: from modules.credits.pricing import pricing
#        price = pricing.MARKUP * api_cost
pricing = PricingConfig()
