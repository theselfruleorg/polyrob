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
import json
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


class TestCacheCreationTokensArePersisted:
    """F17: the number reached calculate_cost and then vanished.

    `usage_records` stored input/output/cached (READS) only, so after the fact
    nothing could say how much of a session's input was a cache WRITE — the one
    figure that decides whether a cache paid for itself, since a write costs
    MORE than an uncached token (1.25x on a 5m window, 2x on a 1h one).
    """

    @pytest.mark.asyncio
    async def test_column_and_metadata_both_carry_the_cache_write(self, tmp_path):
        from modules.database.connection import DatabaseConnection
        from modules.database.auth_tables import AuthTables
        from modules.credits.usage_tracker import build_usage_tracker

        db = DatabaseConnection(tmp_path / "billing.db")
        await db.connect()
        try:
            await AuthTables(db).create_tables()
            # usage_records.user_id has a FK -> user_profiles(user_id); a minimal
            # stand-in row is enough to satisfy PRAGMA foreign_keys=ON.
            await db.execute(
                "CREATE TABLE IF NOT EXISTS user_profiles (user_id TEXT PRIMARY KEY)")
            await db.execute(
                "INSERT OR IGNORE INTO user_profiles (user_id) VALUES ('u1')")

            # The metering-only shape (no balance_manager): records real
            # api_cost_usd, never deducts — the single-owner headless tracker.
            t = build_usage_tracker(db=db, balance_manager=None,
                                    telemetry_manager=None)

            await t.record_llm_usage(
                user_id="u1", session_id="s1", agent_id="a1",
                model=MODEL, provider="anthropic",
                input_tokens=10000, output_tokens=50,
                cached_tokens=1000, cache_creation_tokens=6000,
            )

            row = await db.fetch_one(
                "SELECT cached_tokens, cache_creation_tokens, metadata "
                "FROM usage_records WHERE user_id='u1'")
            assert row["cached_tokens"] == 1000
            assert row["cache_creation_tokens"] == 6000
            md = row["metadata"]
            if isinstance(md, str):
                md = json.loads(md)
            assert md["cache_creation_tokens"] == 6000
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_rollup_sums_the_cache_write(self, tmp_path):
        from modules.database.connection import DatabaseConnection
        from modules.database.auth_tables import AuthTables
        from modules.credits.usage_tracker import build_usage_tracker

        db = DatabaseConnection(tmp_path / "rollup.db")
        await db.connect()
        try:
            await AuthTables(db).create_tables()
            # usage_records.user_id has a FK -> user_profiles(user_id); a minimal
            # stand-in row is enough to satisfy PRAGMA foreign_keys=ON.
            await db.execute(
                "CREATE TABLE IF NOT EXISTS user_profiles (user_id TEXT PRIMARY KEY)")
            await db.execute(
                "INSERT OR IGNORE INTO user_profiles (user_id) VALUES ('u1')")

            t = build_usage_tracker(db=db, balance_manager=None,
                                    telemetry_manager=None)

            for _ in range(3):
                await t.record_llm_usage(
                    user_id="u1", session_id="s1", agent_id="a1",
                    model=MODEL, provider="anthropic",
                    input_tokens=10000, output_tokens=50,
                    cached_tokens=1000, cache_creation_tokens=2000,
                )

            breakdown = await t.get_session_breakdown("s1")
            by_type = breakdown["by_type"][0]
            assert by_type["tokens"]["cached"] == 3000
            assert by_type["tokens"]["cache_write"] == 6000
        finally:
            await db.close()


class TestBilledCostOutranksTheEstimate:
    """F23: a provider that TELLS us what it charged outranks our catalog math.

    None means "not reported" and must leave the estimate alone — a router that
    stayed quiet is not a router that worked for free.
    """

    @pytest.mark.asyncio
    async def test_billed_cost_replaces_the_estimate_and_keeps_it_in_metadata(self, tmp_path):
        from modules.database.connection import DatabaseConnection
        from modules.database.auth_tables import AuthTables
        from modules.credits.usage_tracker import build_usage_tracker

        db = DatabaseConnection(tmp_path / "billed.db")
        await db.connect()
        try:
            await AuthTables(db).create_tables()
            await db.execute(
                "CREATE TABLE IF NOT EXISTS user_profiles (user_id TEXT PRIMARY KEY)")
            await db.execute(
                "INSERT OR IGNORE INTO user_profiles (user_id) VALUES ('u1')")

            t = build_usage_tracker(db=db, balance_manager=None,
                                    telemetry_manager=None)
            record = await t.record_llm_usage(
                user_id="u1", session_id="s1", agent_id="a1",
                model=MODEL, provider="openrouter",
                input_tokens=10000, output_tokens=50,
                billed_cost_usd=0.001234, cache_discount_usd=0.0005,
            )

            assert record.costs.api_cost_usd == pytest.approx(0.001234)
            md = record.metadata or {}
            assert md["estimated_cost_usd"] > 0.001234       # the catalog guess
            assert md["billed_cost_usd"] == pytest.approx(0.001234)
            assert md["cache_discount_usd"] == pytest.approx(0.0005)

            row = await db.fetch_one(
                "SELECT api_cost_usd FROM usage_records WHERE user_id='u1'")
            assert row["api_cost_usd"] == pytest.approx(0.001234)
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_no_billed_cost_leaves_the_estimate_untouched(self):
        from modules.credits.usage_tracker import LLMUsageTracker

        t = LLMUsageTracker.__new__(LLMUsageTracker)
        t.logger = logging.getLogger("f23-none-test")
        tokens = TokenUsage(prompt_tokens=10000, completion_tokens=5000,
                            total_tokens=15000, cached_tokens=0)
        costs = await t._calculate_costs(MODEL, tokens)

        assert t._apply_billed_cost(costs, None) is costs

    @pytest.mark.asyncio
    async def test_unusable_billed_figures_leave_the_estimate_untouched(self):
        from modules.credits.usage_tracker import LLMUsageTracker

        t = LLMUsageTracker.__new__(LLMUsageTracker)
        t.logger = logging.getLogger("f23-bad-test")
        tokens = TokenUsage(prompt_tokens=100, completion_tokens=10,
                            total_tokens=110, cached_tokens=0)
        costs = await t._calculate_costs(MODEL, tokens)

        for bad in (None, True, "n/a", float("nan"), -1.0):
            assert t._apply_billed_cost(costs, bad) is costs

    @pytest.mark.asyncio
    async def test_credits_follow_the_billed_cost(self):
        """Charging a markup on a number the provider contradicted would be the
        same bug in the other direction."""
        from modules.credits.usage_tracker import LLMUsageTracker

        t = LLMUsageTracker.__new__(LLMUsageTracker)
        t.logger = logging.getLogger("f23-credits-test")
        tokens = TokenUsage(prompt_tokens=1_000_000, completion_tokens=0,
                            total_tokens=1_000_000, cached_tokens=0)
        costs = await t._calculate_costs(MODEL, tokens)
        cheaper = t._apply_billed_cost(costs, costs.api_cost_usd / 10.0)

        assert cheaper.api_cost_usd == pytest.approx(costs.api_cost_usd / 10.0)
        assert cheaper.credits_charged < costs.credits_charged
        assert cheaper.markup_multiplier == costs.markup_multiplier
