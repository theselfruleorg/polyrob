"""F23 gap (2026-10-01): the native tool path dropped the billed cost.

`OpenRouterClient._extract_usage_data` builds the usage dict the agent's
native tool path hands to the billing chokepoint. It carried token counts
only, so `next_action_internal` never fell back to `extract_token_usage` (the
dict already had `total_tokens`) and `usage.cost` — what OpenRouter actually
charged — was lost. Measured on prod 10-01: 161/161 rows had no
`billed_cost_usd`, and the stored catalog estimate was ~half the key's real
`usage_daily` ($0.48 vs $0.95), because the cached-read price varies ~10x
across the upstreams OpenRouter routes to.
"""
from types import SimpleNamespace as N

import pytest

from modules.llm.openrouter_client import OpenRouterClient


def _client_with(**usage_fields):
    client = OpenRouterClient.__new__(OpenRouterClient)
    client.last_response = N(usage=N(prompt_tokens=100, completion_tokens=10,
                                     total_tokens=110, **usage_fields))
    return client


def test_native_usage_carries_the_billed_cost():
    out = _client_with(cost=0.0031, cache_discount=0.0004)._extract_usage_data()
    assert out["total_tokens"] == 110
    assert out["billed_cost_usd"] == pytest.approx(0.0031)
    assert out["cache_discount_usd"] == pytest.approx(0.0004)


def test_absent_cost_stays_none_so_the_estimate_is_kept():
    out = _client_with()._extract_usage_data()
    assert out["billed_cost_usd"] is None
    assert out["cache_discount_usd"] is None


def test_no_response_still_returns_the_billed_keys_as_none():
    client = OpenRouterClient.__new__(OpenRouterClient)
    client.last_response = None
    out = client._extract_usage_data()
    assert out.get("billed_cost_usd") is None


def test_billed_cost_survives_the_adapter_into_extract_token_usage():
    """Live proof 15:22Z found the cost dropped a SECOND time: the adapter's
    usage_metadata copied tokens only. Client dict -> adapter usage_metadata ->
    AIMessage -> extract_token_usage must keep billed_cost_usd end to end."""
    from modules.llm.adapters import OpenRouterAdapter
    from modules.llm.messages import AIMessage
    from modules.llm.usage_extract import extract_token_usage

    usage_data = _client_with(cost=0.0042, cache_discount=0.001)._extract_usage_data()
    meta = OpenRouterAdapter._usage_metadata_from(usage_data)
    out = extract_token_usage(AIMessage(content="", usage_metadata=meta), "openrouter")
    assert out["total_tokens"] == 110
    assert out["billed_cost_usd"] == pytest.approx(0.0042)
    assert out["cache_discount_usd"] == pytest.approx(0.001)


def test_adapter_omits_billed_keys_when_the_provider_reported_none():
    from modules.llm.adapters import OpenRouterAdapter
    meta = OpenRouterAdapter._usage_metadata_from(_client_with()._extract_usage_data())
    assert "billed_cost_usd" not in meta


def test_every_agent_billing_call_passes_the_billed_cost():
    """Live proof 16:06Z found the THIRD drop: the native-tools call site (the
    one prod runs, purpose "next_action") called record_llm_usage without
    billed_cost_usd, so the extracted cost never reached the tracker."""
    import ast
    import pathlib

    src = pathlib.Path("agents/task/agent/core/next_action_internal.py").read_text()
    calls = [n for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "record_llm_usage"]
    assert calls
    for call in calls:
        kws = {k.arg for k in call.keywords}
        assert {"billed_cost_usd", "cache_discount_usd"} <= kws, f"line {call.lineno}"
