"""
Usage meter with DYNAMIC token-based pricing.

DEPRECATED: This module is deprecated. Use LLMUsageTracker instead.

LLMUsageTracker provides:
- Unified tracking (DB + Telemetry + Balance deduction in one call)
- Per-call billing (no session finalization needed)
- Complete cost transparency

Migration:
    OLD: await usage_meter.meter_llm_call(...)
    NEW: await usage_tracker.record_llm_usage(...)
"""

import logging
import json
import math
import warnings
from typing import Optional

# Import pricing from THIS module (relative import)
from .pricing import pricing as _pricing_config

logger = logging.getLogger(__name__)


class UsageMeter:
    """
    DEPRECATED: Use LLMUsageTracker instead.

    This class is maintained for backward compatibility only.
    All new code should use modules.credits.LLMUsageTracker.

    The main issue with UsageMeter is that it requires calling
    finalize_session_cost() at session end, which can cause
    double-billing if used alongside LLMUsageTracker.
    """

    # Pricing configuration from centralized config (DO NOT redefine here)
    MARKUP = _pricing_config.MARKUP
    CREDIT_VALUE_USD = _pricing_config.CREDIT_VALUE_USD
    MIN_CREDIT_CHARGE = _pricing_config.MIN_CREDIT_CHARGE

    # Fixed costs for non-LLM resources (from centralized config)
    FIXED_COSTS = {
        "session_creation": _pricing_config.SESSION_CREATION_COST,
        "browser": {
            "page_load": _pricing_config.BROWSER_PAGE_LOAD_COST,
            "screenshot": _pricing_config.BROWSER_SCREENSHOT_COST,
            "interaction": _pricing_config.BROWSER_INTERACTION_COST,
            "default": _pricing_config.BROWSER_DEFAULT_COST
        },
        "special": {
            "vision_call": _pricing_config.VISION_CALL_COST,
            "tool_call": _pricing_config.TOOL_CALL_COST
        }
    }

    def __init__(self, balance_manager):
        """Initialize usage meter."""
        warnings.warn(
            "UsageMeter is deprecated and will be removed in a future version. "
            "Use LLMUsageTracker instead for unified tracking.",
            DeprecationWarning,
            stacklevel=2
        )
        self.balance = balance_manager
        self.db = balance_manager.db
        self.logger = logging.getLogger('credits.usage_meter')

    async def meter_llm_call(
        self,
        user_id: str,
        session_id: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
        provider: str = 'unknown',
        tokens: int = 0  # Backward compatibility
    ) -> int:
        """
        Meter LLM call with DYNAMIC token-based pricing.

        Calculates cost based on actual token usage and real API pricing.

        Args:
            user_id: User identifier
            session_id: Session identifier
            model: Model name (e.g., "gpt-5")
            input_tokens: Number of input/prompt tokens
            output_tokens: Number of output/completion tokens
            cached_tokens: Number of cached input tokens (for DeepSeek)
            provider: Provider name (for logging)
            tokens: DEPRECATED - total tokens for backward compat

        Returns:
            Cost in credits
        """

        # Backward compatibility: estimate split if only total provided
        if input_tokens == 0 and output_tokens == 0 and tokens > 0:
            input_tokens = int(tokens * 0.6)  # Typical 60/40 split
            output_tokens = int(tokens * 0.4)
            self.logger.debug(
                f"Estimated token split from total {tokens}: "
                f"{input_tokens} in, {output_tokens} out"
            )

        # Calculate dynamic cost
        cost_credits, api_cost_usd = await self._calculate_dynamic_cost(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens
        )

        # Record usage with full details
        metadata = json.dumps({
            "model": model,
            "provider": provider,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "api_cost_usd": api_cost_usd,
            "markup": self.MARKUP
        })

        await self.db.execute("""
            INSERT INTO usage_records (
                user_id, session_id, resource_type, cost, metadata, timestamp
            ) VALUES (?, ?, 'llm_call', ?, ?, CURRENT_TIMESTAMP)
        """, (user_id, session_id, cost_credits, metadata))

        self.logger.info(
            f"Metered {model}: {input_tokens:,} in + {output_tokens:,} out "
            f"= ${api_cost_usd:.6f} API → {cost_credits} credits"
        )

        return cost_credits

    async def _calculate_dynamic_cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0
    ) -> tuple[int, float]:
        """
        Calculate cost based on real API pricing.

        Returns:
            Tuple of (credits, api_cost_usd)
        """

        try:
            # Import cost calculator from model registry
            from modules.llm.model_registry import calculate_cost

            # Get real API cost in USD
            api_cost_usd = calculate_cost(
                model_name=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens
            )

            # Convert to credits
            credits_base = api_cost_usd / self.CREDIT_VALUE_USD

            # Apply markup
            credits_with_markup = credits_base * self.MARKUP

            # ALWAYS round UP to ensure we never charge less than API cost
            final_cost = max(self.MIN_CREDIT_CHARGE, math.ceil(credits_with_markup))

            return final_cost, api_cost_usd

        except Exception as e:
            # Fallback to conservative estimate
            self.logger.warning(f"Cost calculation failed for {model}: {e}, using fallback")
            return await self._fallback_estimate(model, input_tokens, output_tokens)

    async def _fallback_estimate(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int
    ) -> tuple[int, float]:
        """Fallback cost estimation if registry lookup fails."""

        total_tokens = input_tokens + output_tokens

        # Rough estimates by model tier
        if "o3" in model or "opus" in model or "gpt-5-pro" in model:
            cost_per_1m = 10.0  # Expensive reasoning models
        elif "gpt-5" in model or "gpt-4" in model or "claude" in model:
            cost_per_1m = 5.0  # Mid-tier models
        elif "gemini" in model or "deepseek" in model or "mini" in model or "nano" in model:
            cost_per_1m = 1.0  # Cheap models
        else:
            cost_per_1m = 3.0  # Conservative default

        api_cost_usd = (total_tokens / 1_000_000) * cost_per_1m
        credits = (api_cost_usd / self.CREDIT_VALUE_USD) * self.MARKUP
        # ALWAYS round UP to ensure we never charge less than API cost
        final_cost = max(self.MIN_CREDIT_CHARGE, math.ceil(credits))

        return final_cost, api_cost_usd

    async def meter_browser_action(
        self,
        user_id: str,
        session_id: str,
        action_type: str
    ) -> int:
        """Meter browser action (fixed cost)."""

        cost = self.FIXED_COSTS["browser"].get(
            action_type,
            self.FIXED_COSTS["browser"]["default"]
        )

        metadata = json.dumps({"action_type": action_type})

        await self.db.execute("""
            INSERT INTO usage_records (
                user_id, session_id, resource_type, cost, metadata, timestamp
            ) VALUES (?, ?, 'browser_action', ?, ?, CURRENT_TIMESTAMP)
        """, (user_id, session_id, cost, metadata))

        return cost

    async def meter_tool_call(
        self,
        user_id: str,
        session_id: str,
        tool_name: str
    ) -> int:
        """Meter tool call (fixed cost)."""

        cost = self.FIXED_COSTS["special"]["tool_call"]

        metadata = json.dumps({"tool_name": tool_name})

        await self.db.execute("""
            INSERT INTO usage_records (
                user_id, session_id, resource_type, cost, metadata, timestamp
            ) VALUES (?, ?, 'tool_call', ?, ?, CURRENT_TIMESTAMP)
        """, (user_id, session_id, cost, metadata))

        return cost

    async def get_session_cost(self, user_id: str, session_id: str) -> int:
        """Get total cost for a session."""

        result = await self.db.fetch_one("""
            SELECT COALESCE(SUM(cost), 0) as total
            FROM usage_records
            WHERE user_id = ? AND session_id = ?
        """, (user_id, session_id))

        return int(result['total']) if result else 0

    async def finalize_session_cost(self, user_id: str, session_id: str) -> int:
        """Calculate total session cost and deduct from balance."""

        total_cost = await self.get_session_cost(user_id, session_id)

        if total_cost > 0:
            success = await self.balance.deduct_credits(
                user_id=user_id,
                amount=total_cost,
                reason=f"Session {session_id} - token-based usage",
                session_id=session_id
            )

            if success:
                self.logger.info(f"Finalized session {session_id}: {total_cost} credits")
            else:
                self.logger.error(f"Failed to deduct {total_cost} credits for {session_id}")

        return total_cost

    async def get_user_usage_summary(self, user_id: str) -> dict:
        """Get usage summary for user."""

        total = await self.db.fetch_one("""
            SELECT COALESCE(SUM(cost), 0) as total
            FROM usage_records WHERE user_id = ?
        """, (user_id,))

        month = await self.db.fetch_one("""
            SELECT COALESCE(SUM(cost), 0) as total
            FROM usage_records
            WHERE user_id = ? AND timestamp >= date('now', 'start of month')
        """, (user_id,))

        by_type = await self.db.fetch_all("""
            SELECT resource_type, COALESCE(SUM(cost), 0) as total
            FROM usage_records WHERE user_id = ?
            GROUP BY resource_type
        """, (user_id,))

        return {
            "total_usage": total['total'] if total else 0,
            "month_usage": month['total'] if month else 0,
            "by_type": {row['resource_type']: row['total'] for row in by_type}
        }
