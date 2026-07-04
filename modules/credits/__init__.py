"""Credit system modules for tracking and metering usage."""

from .balance_manager import CreditBalanceManager
from .usage_tracker import LLMUsageTracker, UsageRecord, CostBreakdown
from .pricing import pricing, PricingConfig, WELCOME_BONUS, DEN_SIGNUP_ALLOWANCE
from .cost_utils import calculate_cost_from_tokens, calculate_user_cost, get_cost_breakdown

# DEPRECATED: UsageMeter - use LLMUsageTracker instead
# Keeping import for backward compatibility during migration
from .usage_meter import UsageMeter

__all__ = [
    # Core components
    'CreditBalanceManager',
    'LLMUsageTracker',  # PRIMARY - use this for LLM tracking
    'UsageRecord',
    'CostBreakdown',
    'pricing',
    'PricingConfig',
    # Credit bonus constants (single source of truth)
    'WELCOME_BONUS',
    'DEN_SIGNUP_ALLOWANCE',
    # Shared utilities
    'calculate_cost_from_tokens',
    'calculate_user_cost',
    'get_cost_breakdown',
    # Deprecated
    'UsageMeter',  # DEPRECATED - will be removed in future version
]
