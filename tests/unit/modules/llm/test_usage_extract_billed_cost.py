"""F23 (2026-09-22): read the cost the provider ACTUALLY billed.

Every cost figure in the tree is otherwise an estimate recomputed from the
model catalog's per-token prices. On OpenRouter that estimate is a guess about
a router: the upstream it picked, its own discounts and its cache pricing are
not in our table, and `cache_discount` is not modelled at all. With
`usage: {"include": true}` on the request the response carries `usage.cost`
(USD) and `usage.cache_discount`.

The load-bearing rule in here: an ABSENT figure is None, never 0.0. "The
provider did not report a cost" and "this call was free" are different facts,
and only one of them may overwrite an estimate at the billing chokepoint.
"""
from types import SimpleNamespace as N

import pytest

from modules.llm.usage_extract import extract_token_usage


def _resp(**usage_fields):
    return N(usage=N(prompt_tokens=100, completion_tokens=10,
                     total_tokens=110, **usage_fields))


def test_billed_cost_and_cache_discount_are_read():
    out = extract_token_usage(_resp(cost=0.0031, cache_discount=0.0004), "openrouter")
    assert out["billed_cost_usd"] == pytest.approx(0.0031)
    assert out["cache_discount_usd"] == pytest.approx(0.0004)


def test_absent_cost_is_none_not_zero():
    out = extract_token_usage(_resp(), "openrouter")
    assert out["billed_cost_usd"] is None
    assert out["cache_discount_usd"] is None


def test_a_genuinely_free_call_reports_zero():
    """0.0 IS a report — a free model on OpenRouter — and must survive as 0.0."""
    out = extract_token_usage(_resp(cost=0.0), "openrouter")
    assert out["billed_cost_usd"] == 0.0


def test_money_is_not_coerced_to_an_integer():
    """The token keys go through int(); the money keys must not — a sub-cent
    cost rounded to 0 would silently zero out a real charge."""
    out = extract_token_usage(_resp(cost=0.0042), "openrouter")
    assert isinstance(out["billed_cost_usd"], float)
    assert out["billed_cost_usd"] == pytest.approx(0.0042)


@pytest.mark.parametrize("bad", ["", "n/a", None, True, float("nan"), -1.0, object()])
def test_unusable_values_are_none(bad):
    out = extract_token_usage(_resp(cost=bad), "openrouter")
    assert out["billed_cost_usd"] is None


def test_dict_shaped_usage_is_read_too():
    out = extract_token_usage({"usage": {"prompt_tokens": 5, "completion_tokens": 1,
                                         "cost": 0.5, "cache_discount": 0.1}},
                              "openrouter")
    assert out["billed_cost_usd"] == pytest.approx(0.5)
    assert out["cache_discount_usd"] == pytest.approx(0.1)


def test_token_counts_are_unaffected():
    out = extract_token_usage(_resp(cost=0.01), "openrouter")
    assert out["prompt_tokens"] == 100 and out["completion_tokens"] == 10


def test_a_response_with_no_usage_block_is_silent():
    out = extract_token_usage(N(), "openrouter")
    assert out["billed_cost_usd"] is None and out["cache_discount_usd"] is None
