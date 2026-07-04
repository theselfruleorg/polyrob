"""Cached-token clamp must be EFFECTIVE (pre-existing no-op fix).

_validate_inputs computed `cached_tokens = min(cached, input)` into a DISCARDED
local, so a cached>input shape (likelier now that A2 surfaces provider cached
metrics) flowed unclamped into TokenUsage/calculate_cost and billed a NEGATIVE
regular-input slice (regular = input - cached). The clamp is now returned and used.
"""
import logging
import pytest

from modules.llm import TokenUsage
from modules.credits.usage_tracker import LLMUsageTracker


def _tracker():
    t = LLMUsageTracker.__new__(LLMUsageTracker)
    t.logger = logging.getLogger("clamp-test")
    return t


def test_validate_inputs_returns_cached_clamped_to_input():
    t = _tracker()
    assert t._validate_inputs(100, 10, 250) == 100   # cached > input -> clamped
    assert t._validate_inputs(100, 10, 50) == 50      # cached <= input -> unchanged
    assert t._validate_inputs(100, 10, 0) == 0


@pytest.mark.asyncio
async def test_cached_over_input_billed_as_fully_cached_no_negative():
    t = _tracker()
    clamped = t._validate_inputs(100, 10, 250)
    assert clamped == 100

    # Cost with the CLAMPED cached (== input) is the fully-cached cost and is
    # non-negative (no negative regular-input slice).
    good = await t._calculate_costs(
        "gpt-4o",
        TokenUsage(prompt_tokens=100, completion_tokens=10, total_tokens=110,
                   cached_tokens=clamped),
    )
    assert good.api_cost_usd >= 0

    # And it matches billing a genuinely fully-cached request (cached == input).
    fully_cached = await t._calculate_costs(
        "gpt-4o",
        TokenUsage(prompt_tokens=100, completion_tokens=10, total_tokens=110,
                   cached_tokens=100),
    )
    assert good.api_cost_usd == fully_cached.api_cost_usd
