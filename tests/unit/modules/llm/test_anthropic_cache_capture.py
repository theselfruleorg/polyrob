from types import SimpleNamespace
from modules.llm.anthropic_client import AnthropicClient
from modules.llm.model_registry import calculate_cost


def _client_with_usage(**usage):
    c = AnthropicClient.__new__(AnthropicClient)          # bypass __init__ (no network)
    import logging; c.logger = logging.getLogger("t"); c.model_type = "claude-sonnet-4-5"
    c.last_response = SimpleNamespace(usage=SimpleNamespace(**usage))
    return c


def test_anthropic_folds_cache_tokens_into_prompt_and_exposes_reads():
    c = _client_with_usage(input_tokens=100, output_tokens=20,
                           cache_read_input_tokens=900, cache_creation_input_tokens=50)
    u = c._extract_usage_data()
    assert u["prompt_tokens"] == 1050                     # 100 + 900 + 50
    assert u["cached_tokens"] == 900                      # reads only
    assert u["completion_tokens"] == 20


def test_anthropic_no_cache_fields_unchanged():
    # Old-shape usage (no cache attrs) must behave exactly as before.
    c = _client_with_usage(input_tokens=100, output_tokens=20)
    u = c._extract_usage_data()
    assert u["prompt_tokens"] == 100 and u["completion_tokens"] == 20
    assert u["total_tokens"] == 120
    assert u.get("cached_tokens", 0) == 0


def test_cost_uses_cached_rate_for_anthropic_reads():
    # sonnet-4-5: input $3, cached $0.30 (after A1). 900 cached reads + 150 regular input.
    cost = calculate_cost("claude-sonnet-4-5", input_tokens=1050, output_tokens=20, cached_tokens=900)
    # regular = 1050-900 = 150 @ $3/M; cached = 900 @ $0.30/M
    assert round(cost, 8) == round(150/1e6*3.0 + 900/1e6*0.3 + 20/1e6*15.0, 8)
