"""
G3/G4 (telemetry audit 2026-07-04): cache-write pricing + cached-price invariant.

G3: Anthropic bills cache *creation* (write) tokens at 1.25x input, but
`calculate_cost` folded them into regular input at 1x — a systematic undercharge
on cache-heavy Anthropic sessions. `calculate_cost` must accept
`cache_creation_tokens` and bill them at the model's `cache_write_price`.

G4: every registered model with pricing must have a non-None `cached_input_price`
(derived at the provider multiplier in __post_init__) so cached tokens can never
silently bill at $0.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from modules.llm.model_registry import (
    calculate_cost,
    get_model_config,
    get_all_models,
)


class TestCacheWritePricingG3:
    def test_cache_creation_tokens_surcharged_at_1_25x_for_anthropic(self):
        """Cache-write tokens cost 1.25x input, not 1x.

        claude-sonnet-4-5: input $3/M, output $15/M.
        10k input (4k of which are cache-creation), 5k output.
          regular  = 6000/1M * 3.00            = 0.018
          write    = 4000/1M * (3.00 * 1.25)   = 0.015
          output   = 5000/1M * 15.00           = 0.075
          total                                = 0.108
        Without the fix (write folded into input at 1x) it would be 0.105.
        """
        cost = calculate_cost(
            "claude-sonnet-4-5",
            input_tokens=10000,
            output_tokens=5000,
            cached_tokens=0,
            cache_creation_tokens=4000,
        )
        assert abs(cost - 0.108) < 1e-6, f"expected 0.108, got {cost}"

    def test_cache_creation_costs_more_than_plain_input(self):
        cost_plain = calculate_cost("claude-sonnet-4-5", 10000, 5000, 0, 0)
        cost_write = calculate_cost("claude-sonnet-4-5", 10000, 5000, 0, 4000)
        assert cost_write > cost_plain

    def test_cache_creation_default_zero_is_backward_compatible(self):
        """Omitting cache_creation_tokens must be byte-identical to before."""
        cost_new = calculate_cost("claude-sonnet-4-5", 10000, 5000, 0)
        # $3/M*10k + $15/M*5k = 0.03 + 0.075
        assert abs(cost_new - 0.105) < 1e-6


class TestCacheWriteThreadingG3:
    """The cache-creation count must travel from the response to the biller."""

    def test_extract_token_usage_reads_cache_creation(self):
        from agents.task.utils import extract_token_usage

        class _Resp:
            usage_metadata = {
                "input_tokens": 10000,
                "output_tokens": 5000,
                "total_tokens": 15000,
                "cache_read_input_tokens": 2000,
                "cache_creation_input_tokens": 4000,
            }

        tu = extract_token_usage(_Resp(), "anthropic")
        assert tu.get("cache_creation_tokens") == 4000
        assert tu.get("cached_tokens") == 2000

    @__import__("pytest").mark.asyncio
    async def test_calculate_costs_bills_cache_creation_surcharge(self):
        import logging
        from modules.llm import TokenUsage
        from modules.credits.usage_tracker import LLMUsageTracker

        t = LLMUsageTracker.__new__(LLMUsageTracker)
        t.logger = logging.getLogger("g3-thread-test")

        plain = await t._calculate_costs(
            "claude-sonnet-4-5",
            TokenUsage(prompt_tokens=10000, completion_tokens=5000,
                       total_tokens=15000, cached_tokens=0),
        )
        with_write = await t._calculate_costs(
            "claude-sonnet-4-5",
            TokenUsage(prompt_tokens=10000, completion_tokens=5000,
                       total_tokens=15000, cached_tokens=0,
                       cache_creation_tokens=4000),
        )
        assert with_write.api_cost_usd > plain.api_cost_usd
        assert abs(with_write.api_cost_usd - 0.108) < 1e-6


class TestCachedPriceInvariantG4:
    def test_every_registered_model_with_pricing_has_cached_price(self):
        """No registered model may leave cached_input_price None → $0 cached."""
        offenders = []
        for cfg in get_all_models(include_deprecated=True):
            if cfg.pricing is None:
                continue
            if cfg.pricing.input_price and cfg.pricing.cached_input_price is None:
                offenders.append(cfg.name)
        assert not offenders, f"models with None cached_input_price: {offenders}"
