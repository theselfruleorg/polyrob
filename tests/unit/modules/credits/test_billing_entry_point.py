"""G-24 (Task 5a): fragmented cost computation loses the cache-write surcharge.

`modules/llm/model_registry.py::calculate_cost` bills three slices: regular
input, cached-read (discounted), and cache-WRITE (Anthropic 1.25x surcharge).
Before this fix, only `LLMUsageTracker._calculate_costs` forwarded
`cache_creation_tokens` to `calculate_cost` -- every other caller
(`cost_utils.calculate_cost_from_tokens` and its transitive callers in
telemetry/webview display code) silently dropped it, undercharging the
DISPLAYED cost estimate on cache-heavy Anthropic sessions (though NOT the
real billed ledger row, which already routed through the tracker).

This test module locks in:
1. A single reusable entry point, `modules/credits/pricing.py::compute_llm_cost`,
   that always forwards cached_tokens AND cache_creation_tokens, accepting
   either a TokenUsage-like object or a plain dict.
2. `cost_utils.calculate_cost_from_tokens` (the estimate/display path) now
   also accepts and forwards `cache_creation_tokens` instead of dropping it.
3. `LLMUsageTracker._calculate_costs` (the real billing path) is routed
   through the same `compute_llm_cost` entry point, so a duplicate/divergent
   billing formula can't reappear.
"""
import logging

import pytest

from modules.credits.pricing import compute_llm_cost
from modules.credits.cost_utils import calculate_cost_from_tokens
from modules.llm import TokenUsage


MODEL = "claude-sonnet-4-5"  # $3/M in, $15/M out, cache_write ~= 1.25x input


class TestComputeLlmCostEntryPoint:
    def test_cache_creation_tokens_increase_cost_via_object(self):
        """A TokenUsage object with cache_creation_tokens > 0 must cost more
        than the same object with cache_creation_tokens == 0 -- this is
        exactly the case that silently no-opped under the old dropped-param
        behavior (fragmented callers ignored the field entirely)."""
        plain = TokenUsage(prompt_tokens=10000, completion_tokens=5000,
                            total_tokens=15000, cached_tokens=0,
                            cache_creation_tokens=0)
        with_write = TokenUsage(prompt_tokens=10000, completion_tokens=5000,
                                 total_tokens=15000, cached_tokens=0,
                                 cache_creation_tokens=4000)

        cost_plain = compute_llm_cost(MODEL, plain)
        cost_write = compute_llm_cost(MODEL, with_write)

        assert cost_write > cost_plain
        assert abs(cost_write - 0.108) < 1e-6  # matches calculate_cost's own G3 math

    def test_cache_creation_tokens_increase_cost_via_dict(self):
        """Same as above, but usage passed as a plain dict (e.g. a
        provider-response-derived dict rather than a TokenUsage instance)."""
        plain = {"prompt_tokens": 10000, "completion_tokens": 5000,
                 "cached_tokens": 0, "cache_creation_tokens": 0}
        with_write = {"prompt_tokens": 10000, "completion_tokens": 5000,
                      "cached_tokens": 0, "cache_creation_tokens": 4000}

        assert compute_llm_cost(MODEL, with_write) > compute_llm_cost(MODEL, plain)

    def test_dict_accepts_input_output_token_aliases(self):
        """Some callers use input_tokens/output_tokens naming instead of
        prompt_tokens/completion_tokens -- the entry point must handle both."""
        usage = {"input_tokens": 10000, "output_tokens": 5000,
                 "cached_tokens": 0, "cache_creation_tokens": 4000}
        cost = compute_llm_cost(MODEL, usage)
        assert abs(cost - 0.108) < 1e-6

    def test_missing_cache_creation_defaults_to_zero(self):
        """Omitting cache_creation_tokens entirely must be byte-identical to
        passing zero (no silent regression for callers that genuinely have
        no cache-write data, e.g. non-Anthropic providers)."""
        usage = {"prompt_tokens": 10000, "completion_tokens": 5000}
        cost = compute_llm_cost(MODEL, usage)
        assert abs(cost - 0.105) < 1e-6


class TestCalculateCostFromTokensForwardsCacheCreation:
    """The estimate/display path (cost_utils.calculate_cost_from_tokens) must
    no longer silently drop cache_creation_tokens when a caller has it."""

    def test_cache_creation_tokens_changes_result(self):
        cost_plain = calculate_cost_from_tokens(
            model_name=MODEL, input_tokens=10000, output_tokens=5000,
            cached_tokens=0, cache_creation_tokens=0,
        )
        cost_write = calculate_cost_from_tokens(
            model_name=MODEL, input_tokens=10000, output_tokens=5000,
            cached_tokens=0, cache_creation_tokens=4000,
        )
        assert cost_write > cost_plain
        assert abs(cost_write - 0.108) < 1e-6

    def test_omitting_cache_creation_is_backward_compatible(self):
        """Existing callers that don't pass cache_creation_tokens (display/
        estimate-only call sites outside this task's scope) must see
        byte-identical output to before this change."""
        cost = calculate_cost_from_tokens(
            model_name=MODEL, input_tokens=10000, output_tokens=5000,
            cached_tokens=0,
        )
        assert abs(cost - 0.105) < 1e-6


class TestUsageTrackerRoutesThroughSingleEntryPoint:
    """LLMUsageTracker._calculate_costs (the REAL billing path -- the one that
    feeds credit deduction + the usage_records ledger row) must be wired to
    the same compute_llm_cost entry point as every other caller, so the
    billing formula can never fork again."""

    @pytest.mark.asyncio
    async def test_calculate_costs_matches_compute_llm_cost(self):
        from modules.credits.usage_tracker import LLMUsageTracker

        t = LLMUsageTracker.__new__(LLMUsageTracker)
        t.logger = logging.getLogger("g24-entry-point-test")

        tokens = TokenUsage(prompt_tokens=10000, completion_tokens=5000,
                             total_tokens=15000, cached_tokens=0,
                             cache_creation_tokens=4000)
        breakdown = await t._calculate_costs(MODEL, tokens)

        assert breakdown.api_cost_usd == compute_llm_cost(MODEL, tokens)
        assert abs(breakdown.api_cost_usd - 0.108) < 1e-6

    @pytest.mark.asyncio
    async def test_calculate_costs_still_bills_cache_creation_surcharge(self):
        """Regression guard for the pre-existing G3 behavior (must survive
        the refactor to route through compute_llm_cost)."""
        from modules.credits.usage_tracker import LLMUsageTracker

        t = LLMUsageTracker.__new__(LLMUsageTracker)
        t.logger = logging.getLogger("g24-regression-test")

        plain = await t._calculate_costs(
            MODEL,
            TokenUsage(prompt_tokens=10000, completion_tokens=5000,
                       total_tokens=15000, cached_tokens=0),
        )
        with_write = await t._calculate_costs(
            MODEL,
            TokenUsage(prompt_tokens=10000, completion_tokens=5000,
                       total_tokens=15000, cached_tokens=0,
                       cache_creation_tokens=4000),
        )
        assert with_write.api_cost_usd > plain.api_cost_usd
