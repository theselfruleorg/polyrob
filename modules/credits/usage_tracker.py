"""
Unified LLM Usage Tracking Service

SINGLE SOURCE OF TRUTH for all token tracking, cost calculation, and billing.
Replaces fragmented tracking across usage_meter and telemetry.

This tracker:
- Validates token data
- Calculates costs (API + markup) using model_registry
- Writes to database (primary storage)
- Writes to telemetry (UI display)
- Deducts from user balance
- Maintains complete audit trail
- Supports fail-fast billing (raises InsufficientCreditsError)

INTEGRATION:
- Uses modules.llm.TokenUsage for token data (no duplication)
- Uses modules.llm.model_registry.calculate_cost for API pricing
- Adds configurable markup for user billing (via PRICING_MARKUP env var, default 1.0 = 0%)
"""

from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any
import math
import uuid
import json
import logging
import os
from datetime import datetime

# Import LLM module's TokenUsage (don't duplicate!)
from modules.llm import TokenUsage, calculate_cost

# Import pricing from THIS module (relative import to avoid circular dependencies)
from .pricing import pricing as _pricing_config

logger = logging.getLogger(__name__)


# Canonical definition lives in core.exceptions so agent-side code can raise
# and catch it without depending on this billing module (which is server-scope).
from core.exceptions import InsufficientCreditsError  # noqa: F401


@dataclass
class CostBreakdown:
    """Detailed cost breakdown for transparency."""
    api_cost_usd: float  # What we pay the API
    markup_multiplier: float  # Our markup (1.20 = 20%)
    credits_raw: float  # Before rounding
    credits_charged: int  # What user pays (in credits)
    user_cost_usd: float  # What user pays in USD (credits × $0.01)

    @property
    def markup_amount_usd(self) -> float:
        """How much markup we added."""
        return self.user_cost_usd - self.api_cost_usd

    @property
    def savings_from_cache_usd(self) -> float:
        """How much saved from caching."""
        # Would need cached token pricing to calculate
        return 0.0


@dataclass
class UsageRecord:
    """Complete usage record for LLM call."""
    # Identity
    request_id: str
    user_id: str
    session_id: str
    agent_id: str
    timestamp: float

    # Model info
    model: str
    provider: str

    # Token data
    tokens: TokenUsage

    # Cost data
    costs: CostBreakdown

    # Execution info
    duration_seconds: float
    component: str
    purpose: str
    success: bool
    error: Optional[str] = None

    # Metadata
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            **asdict(self),
            'tokens': asdict(self.tokens),
            'costs': asdict(self.costs)
        }


class LLMUsageTracker:
    """
    Unified service for tracking ALL LLM usage.

    RESPONSIBILITIES:
    1. Validate token data
    2. Calculate costs (API + markup)
    3. Write to database (primary storage)
    4. Write to telemetry (UI display)
    5. Deduct from user balance
    6. Maintain complete audit trail

    GUARANTEES:
    - Single write path (atomic)
    - Consistent data everywhere
    - Accurate billing
    - Complete transparency

    BILLING MODES:
    - fail_on_insufficient=True: Raises InsufficientCreditsError, stops execution
    - fail_on_insufficient=False: Logs warning, records failure, continues execution
    """

    # Pricing configuration from centralized config (DO NOT redefine here)
    MARKUP = _pricing_config.MARKUP
    CREDIT_VALUE_USD = _pricing_config.CREDIT_VALUE_USD
    MIN_CREDIT_CHARGE = _pricing_config.MIN_CREDIT_CHARGE

    def __init__(self, db, balance_manager, telemetry_manager, fail_on_insufficient: bool = None):
        """
        Initialize usage tracker.

        Args:
            db: Database connection
            balance_manager: CreditBalanceManager instance
            telemetry_manager: ProductTelemetry instance
            fail_on_insufficient: If True, raises InsufficientCreditsError when credits run out.
                                  Defaults to env var FAIL_ON_INSUFFICIENT_CREDITS (default: True)
        """
        self.db = db
        self.balance = balance_manager
        self.telemetry = telemetry_manager
        self.logger = logging.getLogger('credits.usage_tracker')

        # Billing enforcement mode
        if fail_on_insufficient is not None:
            self.fail_on_insufficient = fail_on_insufficient
        else:
            self.fail_on_insufficient = os.environ.get(
                "FAIL_ON_INSUFFICIENT_CREDITS", "true"
            ).lower() == "true"

        # x402 hard token cap: an x402 request prepaid for a bounded token budget
        # (the price = budget × max-model-rate × markup). Accumulate per session so a
        # single request can't run away past what it paid for. In-memory (the run is
        # in-process); bounded to avoid unbounded growth on a long-lived server.
        self._x402_session_tokens: Dict[str, int] = {}
        # Cache the user tier so we don't hit the DB on every LLM call.
        self._tier_cache: Dict[str, str] = {}

        self.logger.info(f"LLMUsageTracker initialized with fail_on_insufficient={self.fail_on_insufficient}")

    async def _get_user_tier(self, user_id: str) -> str:
        """Return the user's tier ('x402'/'admin'/…), cached to avoid a per-call query."""
        if user_id in self._tier_cache:
            return self._tier_cache[user_id]
        tier = ""
        try:
            result = await self.db.fetch_one(
                "SELECT tier FROM user_profiles WHERE user_id = ?", (user_id,)
            )
            if result:
                tier = result["tier"] or ""
        except Exception as e:
            self.logger.debug(f"tier lookup failed for {user_id}: {e}")
        if len(self._tier_cache) > 10000:  # bound the cache
            self._tier_cache.clear()
        self._tier_cache[user_id] = tier
        return tier

    def _enforce_x402_budget(self, user_id: str, session_id: str, tokens_this_call: int) -> None:
        """Halt an x402 request once its cumulative tokens exceed the prepaid budget.

        x402 settles a fixed price BEFORE the run (pay-before-serve); that price covers
        X402_MAX_TOKENS_PER_REQUEST tokens (× max-model-rate × markup). This cap makes
        sure actual usage can't exceed the budget, so the platform can never spend more
        on a request than it collected. Per-session (one x402 session = one bounded
        request); raises InsufficientCreditsError, which the step loop treats as fatal.
        """
        from modules.x402.x402_integration import get_x402_max_tokens_per_request
        budget = get_x402_max_tokens_per_request()
        if budget <= 0:
            return
        # Bound the accumulator dict before inserting a new session.
        if session_id not in self._x402_session_tokens and len(self._x402_session_tokens) > 10000:
            self._x402_session_tokens.clear()
        total = self._x402_session_tokens.get(session_id, 0) + max(0, int(tokens_this_call or 0))
        self._x402_session_tokens[session_id] = total
        if total > budget:
            raise InsufficientCreditsError(
                user_id=user_id,
                required=total,
                available=budget,
                message=(
                    f"x402 prepaid token budget exhausted for this request: "
                    f"{total} > {budget} tokens. This request was priced for {budget} tokens — "
                    f"start a new paid request or raise X402_MAX_TOKENS_PER_REQUEST."
                ),
            )

    async def record_llm_usage(
        self,
        user_id: str,
        session_id: str,
        agent_id: str,
        model: str,
        provider: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
        duration_seconds: float = 0,
        component: str = "agent",
        purpose: str = "inference",
        success: bool = True,
        error: Optional[str] = None,
        metadata: Optional[Dict] = None,
        cache_creation_tokens: int = 0
    ) -> UsageRecord:
        """
        Record LLM usage across ALL systems atomically.

        This is the ONLY method that should be called to track LLM usage.
        It replaces separate calls to usage_meter and telemetry.

        Args:
            user_id: User identifier
            session_id: Session identifier
            agent_id: Agent identifier
            model: Model name (e.g., "gpt-4")
            provider: Provider name (e.g., "openai")
            input_tokens: Prompt/input tokens
            output_tokens: Completion/output tokens
            cached_tokens: Cached prompt tokens (for prompt caching)
            duration_seconds: LLM call duration
            component: Component making the call (e.g., "agent")
            purpose: Purpose of call (e.g., "next_action")
            success: Whether call succeeded
            error: Error message if failed
            metadata: Additional metadata

        Returns:
            UsageRecord with complete billing information
        """
        try:
            # 1. Validate inputs (returns cached_tokens CLAMPED to input_tokens).
            # A2 (provider cached-token metrics) makes cached>input shapes likelier;
            # an unclamped cached>input would bill a NEGATIVE regular-input slice in
            # calculate_cost. The clamp was previously computed into a discarded
            # local — apply it here so the TokenUsage below is correct.
            cached_tokens = self._validate_inputs(input_tokens, output_tokens, cached_tokens)

            # G3: clamp cache-creation so regular input (input - cached - creation)
            # can never go negative on a mis-reported split.
            cache_creation_tokens = max(0, min(cache_creation_tokens or 0,
                                               max(0, input_tokens - cached_tokens)))

            # 2. Create token usage object (using LLM module's TokenUsage)
            tokens = TokenUsage(
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                cached_tokens=cached_tokens,
                cache_creation_tokens=cache_creation_tokens
            )

            # 3. Calculate costs (SINGLE SOURCE OF TRUTH)
            costs = await self._calculate_costs(model, tokens)

            # 4. Generate unique request ID
            request_id = self._generate_request_id()

            # 5. Create complete usage record
            record = UsageRecord(
                request_id=request_id,
                user_id=user_id,
                session_id=session_id,
                agent_id=agent_id,
                timestamp=datetime.now().timestamp(),
                model=model,
                provider=provider,
                tokens=tokens,
                costs=costs,
                duration_seconds=duration_seconds,
                component=component,
                purpose=purpose,
                success=success,
                error=error,
                metadata=metadata
            )

            # H3: settle the per-token charge for normal users BEFORE persisting the
            # usage record, so a fail-fast credit halt doesn't leave an overstated
            # "charged" usage_records row with no matching deduction (get_session_breakdown
            # sums that row). x402/admin ledger + budget enforcement stay AFTER the write
            # (order preserved). _deduct_from_balance records a billing_failure on failure.
            charged_tier = None
            if costs.credits_charged > 0:
                charged_tier = await self._get_user_tier(user_id)
                if charged_tier not in ("x402", "admin"):
                    await self._deduct_from_balance(record)

            # 6. Write to database (primary source of truth)
            await self._write_to_database(record)

            # 7. Write to telemetry (for real-time UI)
            await self._write_to_telemetry(record)

            # 8. Non-charged tiers (x402/admin don't pay per-token): ledger the usage so
            #    it's reconcilable/auditable, and enforce the x402 prepaid token budget.
            if costs.credits_charged > 0 and charged_tier in ("x402", "admin"):
                self.logger.debug(f"Skipping credit deduction for {charged_tier} user {user_id}")
                await self._record_usage_ledger(record)
                if charged_tier == "x402":
                    self._enforce_x402_budget(user_id, session_id, tokens.total_tokens)

            # 9. Log for monitoring (using LLM module's TokenUsage properties)
            self.logger.info(
                f"✓ Tracked {model}: {tokens.prompt_tokens}+{tokens.completion_tokens}={tokens.total_tokens} tokens, "
                f"API=${costs.api_cost_usd:.6f} → User charged ${costs.user_cost_usd:.6f} "
                f"({costs.credits_charged} credits)"
            )

            return record

        except InsufficientCreditsError:
            # A billing halt (credit exhaustion or x402 budget cap) is not a recording
            # failure — propagate it cleanly without the misleading error log.
            raise
        except Exception as e:
            self.logger.error(f"Failed to record LLM usage: {e}", exc_info=True)
            raise

    def _validate_inputs(self, input_tokens: int, output_tokens: int, cached_tokens: int) -> int:
        """Validate token counts. Returns cached_tokens clamped to input_tokens.

        The caller MUST use the returned value: cached_tokens > input_tokens would
        otherwise flow into TokenUsage/calculate_cost and bill a negative
        regular-input slice (regular = input - cached).
        """
        if input_tokens < 0:
            raise ValueError(f"input_tokens cannot be negative: {input_tokens}")
        if output_tokens < 0:
            raise ValueError(f"output_tokens cannot be negative: {output_tokens}")
        if cached_tokens < 0:
            raise ValueError(f"cached_tokens cannot be negative: {cached_tokens}")
        if cached_tokens > input_tokens:
            self.logger.warning(
                f"cached_tokens ({cached_tokens}) > input_tokens ({input_tokens}), "
                "clamping to input_tokens"
            )
            cached_tokens = min(cached_tokens, input_tokens)
        return cached_tokens

    async def _calculate_costs(self, model: str, tokens: TokenUsage) -> CostBreakdown:
        """
        Calculate costs with full transparency.

        Uses modules.llm.model_registry.calculate_cost for API pricing,
        then adds configurable markup for user billing (PRICING_MARKUP env var).

        Returns complete breakdown: API cost → markup → credits → user cost
        """
        # Calculate API cost using centralized registry (already imported)
        api_cost_usd = calculate_cost(
            model_name=model,
            input_tokens=tokens.prompt_tokens,
            output_tokens=tokens.completion_tokens,
            cached_tokens=tokens.cached_tokens,
            cache_creation_tokens=getattr(tokens, "cache_creation_tokens", 0),
        )

        # Convert to credits with markup
        credits_raw = (api_cost_usd / self.CREDIT_VALUE_USD) * self.MARKUP

        # ALWAYS round UP to ensure we never charge less than API cost
        # Using math.ceil() instead of int(x + 0.5) which was incorrectly rounding DOWN
        # for values like 2.42 → int(2.92) = 2, losing money!
        credits_charged = max(self.MIN_CREDIT_CHARGE, math.ceil(credits_raw))

        # Calculate what user actually pays
        user_cost_usd = credits_charged * self.CREDIT_VALUE_USD

        return CostBreakdown(
            api_cost_usd=api_cost_usd,
            markup_multiplier=self.MARKUP,
            credits_raw=credits_raw,
            credits_charged=credits_charged,
            user_cost_usd=user_cost_usd
        )

    def _generate_request_id(self) -> str:
        """Generate unique request ID for deduplication."""
        return uuid.uuid4().hex

    async def _write_to_database(self, record: UsageRecord):
        """
        Write complete usage record to database.

        Uses NEW token columns + stores full breakdown in metadata.
        """
        # Build metadata with full cost breakdown
        metadata_dict = {
            "request_id": record.request_id,
            "model": record.model,
            "provider": record.provider,
            "agent_id": record.agent_id,
            "component": record.component,
            "purpose": record.purpose,
            "duration_seconds": record.duration_seconds,
            "success": record.success,
            "error": record.error,
            # Full cost breakdown for transparency
            "credits_raw": record.costs.credits_raw,
            "user_cost_usd": record.costs.user_cost_usd,
            **(record.metadata or {})
        }

        await self.db.execute("""
            INSERT INTO usage_records (
                user_id, session_id, resource_type,
                cost, input_tokens, output_tokens, cached_tokens,
                api_cost_usd, markup_multiplier,
                metadata, timestamp
            ) VALUES (?, ?, 'llm_call', ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            record.user_id,
            record.session_id,
            record.costs.credits_charged,  # Credits charged to user
            record.tokens.prompt_tokens,  # LLM module uses prompt_tokens
            record.tokens.completion_tokens,  # LLM module uses completion_tokens
            record.tokens.cached_tokens,
            record.costs.api_cost_usd,
            record.costs.markup_multiplier,
            json.dumps(metadata_dict)
        ))

    async def _write_to_telemetry(self, record: UsageRecord):
        """
        Write to telemetry feed for real-time UI updates.

        IMPORTANT: Includes BOTH api_cost and user_cost for proper display!
        """
        if not self.telemetry:
            return

        try:
            # Capture with FULL cost information
            # NOTE: capture_llm_usage is NOT async, don't await it
            self.telemetry.capture_llm_usage(
                component=record.component,
                purpose=record.purpose,
                model_name=record.model,
                duration_seconds=record.duration_seconds,
                success=record.success,
                token_count=record.tokens.total_tokens,
                prompt_tokens=record.tokens.prompt_tokens,
                completion_tokens=record.tokens.completion_tokens,
                cached_tokens=record.tokens.cached_tokens,
                agent_id=record.agent_id,
                parameters={
                    "request_id": record.request_id,
                    "provider": record.provider,
                    # CRITICAL: Include both costs for UI
                    "api_cost_usd": record.costs.api_cost_usd,
                    "user_cost_usd": record.costs.user_cost_usd,  # What they're charged
                    "credits_charged": record.costs.credits_charged,
                    "markup": record.costs.markup_multiplier
                }
            )
        except Exception as e:
            self.logger.warning(f"Failed to write telemetry (non-critical): {e}")

    async def _should_deduct_credits(self, user_id: str) -> bool:
        """Check if we should deduct credits from this user.

        x402 users pay per-request via cryptocurrency signature, not via credits.
        Admin users also bypass credit deduction.

        Args:
            user_id: User to check

        Returns:
            True if credits should be deducted, False to skip
        """
        # Routes through the cached tier lookup (SSOT for tier); x402/admin are exempt.
        # On lookup failure _get_user_tier returns "" -> deduct (fail-safe).
        tier = await self._get_user_tier(user_id)
        return tier not in ("x402", "admin")

    async def _deduct_from_balance(self, record: UsageRecord):
        """Deduct credits from user balance with configurable enforcement.

        Args:
            record: UsageRecord containing billing information

        Raises:
            InsufficientCreditsError: If fail_on_insufficient=True and deduction fails
        """
        success = await self.balance.deduct_credits(
            user_id=record.user_id,
            amount=record.costs.credits_charged,
            reason=(
                f"LLM: {record.model} "
                f"({record.tokens.prompt_tokens}+{record.tokens.completion_tokens} tokens)"
            ),
            session_id=record.session_id
        )

        if not success:
            self.logger.error(
                f"❌ Failed to deduct {record.costs.credits_charged} credits "
                f"from user {record.user_id} - BILLING ISSUE!"
            )

            if self.fail_on_insufficient:
                # H3: leave a reconciliation trail on the fail-fast path too — a
                # charge that never deducted must still be recorded in
                # billing_failures (matches the soft-fail branch + the documented
                # "Billing Failures tracked for admin reconciliation" guarantee).
                await self._record_billing_failure(record)

                # FAIL-FAST: Stop execution immediately
                # Get current balance for error message
                balance_info = await self.balance.get_balance(record.user_id)
                available = balance_info.get('balance', 0) if balance_info else 0

                raise InsufficientCreditsError(
                    user_id=record.user_id,
                    required=record.costs.credits_charged,
                    available=available
                )
            else:
                # SOFT-FAIL: Log and continue (current behavior)
                # Record billing failure for later reconciliation
                await self._record_billing_failure(record)

    async def _record_usage_ledger(self, record: UsageRecord):
        """Ledger usage for non-charged tiers (x402/admin) — F14.

        Fail-open: a ledger write failure must never break the request (the work
        already happened). Enables reconciliation/back-billing + admin cost audit.
        """
        try:
            await self.db.execute("""
                CREATE TABLE IF NOT EXISTS usage_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    session_id TEXT,
                    request_id TEXT,
                    model TEXT,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    api_cost_usd REAL,
                    credits_charged INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await self.db.execute("""
                INSERT INTO usage_ledger (
                    user_id, session_id, request_id, model,
                    prompt_tokens, completion_tokens, api_cost_usd, credits_charged
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record.user_id,
                record.session_id,
                record.request_id,
                record.model,
                record.tokens.prompt_tokens,
                record.tokens.completion_tokens,
                record.costs.api_cost_usd,
                record.costs.credits_charged,
            ))
        except Exception as e:
            self.logger.error(f"Failed to record usage ledger: {e}")

    async def _record_billing_failure(self, record: UsageRecord):
        """Record billing failure for later reconciliation.

        This creates an entry in the billing_failures table so admins
        can review and resolve unpaid charges.
        """
        try:
            await self.db.execute("""
                INSERT INTO billing_failures (
                    user_id, session_id, request_id,
                    credits_owed, api_cost_usd, model,
                    created_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 'pending')
            """, (
                record.user_id,
                record.session_id,
                record.request_id,
                record.costs.credits_charged,
                record.costs.api_cost_usd,
                record.model
            ))
            self.logger.warning(
                f"📝 Recorded billing failure: user={record.user_id}, "
                f"credits_owed={record.costs.credits_charged}, request={record.request_id}"
            )
        except Exception as e:
            self.logger.error(f"Failed to record billing failure: {e}")

    async def get_session_breakdown(self, session_id: str) -> Dict[str, Any]:
        """
        Get detailed cost breakdown for a session.

        Returns what user was charged vs API costs.
        """
        records = await self.db.fetch_all("""
            SELECT
                resource_type,
                SUM(cost) as total_credits,
                SUM(input_tokens) as total_input,
                SUM(output_tokens) as total_output,
                SUM(cached_tokens) as total_cached,
                SUM(api_cost_usd) as total_api_cost,
                COUNT(*) as call_count
            FROM usage_records
            WHERE session_id = ?
            GROUP BY resource_type
        """, (session_id,))

        breakdown = {
            "session_id": session_id,
            "total_credits_charged": 0,
            "total_user_cost_usd": 0,
            "total_api_cost_usd": 0,
            "total_markup_usd": 0,
            "by_type": []
        }

        for record in records:
            user_cost = record['total_credits'] * self.CREDIT_VALUE_USD
            markup = user_cost - (record['total_api_cost'] or 0)

            breakdown["total_credits_charged"] += record['total_credits']
            breakdown["total_user_cost_usd"] += user_cost
            breakdown["total_api_cost_usd"] += record['total_api_cost'] or 0
            breakdown["total_markup_usd"] += markup

            breakdown["by_type"].append({
                "type": record['resource_type'],
                "calls": record['call_count'],
                "tokens": {
                    "input": record['total_input'] or 0,
                    "output": record['total_output'] or 0,
                    "cached": record['total_cached'] or 0
                },
                "credits_charged": record['total_credits'],
                "user_cost_usd": user_cost,
                "api_cost_usd": record['total_api_cost'] or 0,
                "markup_usd": markup
            })

        return breakdown


# Export for use
__all__ = ['LLMUsageTracker', 'UsageRecord', 'TokenUsage', 'CostBreakdown', 'InsufficientCreditsError']
