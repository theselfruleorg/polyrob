"""UP-08 Steps 8.2-8.4 — providers surface cached-token counts so billing applies the
cached_input rate. DeepSeek (dict usage), OpenRouter/NIM (object usage), Gemini (usage_metadata).
"""
import types

from modules.llm.deepseek_client import DeepSeekClient
from modules.llm.openrouter_client import OpenRouterClient
from modules.llm.gemini_client import GeminiClient


def _bare(cls):
    obj = object.__new__(cls)
    import logging
    obj.logger = logging.getLogger("cached-tokens-test")
    return obj


def test_deepseek_surfaces_prompt_cache_hit_tokens():
    c = _bare(DeepSeekClient)
    c.last_response = {"usage": {
        "prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050,
        "prompt_cache_hit_tokens": 512,
    }}
    usage = c._extract_usage_data()
    assert usage["cached_tokens"] == 512
    assert usage["prompt_tokens"] == 1000


def test_deepseek_no_cache_field_defaults_zero():
    c = _bare(DeepSeekClient)
    c.last_response = {"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
    usage = c._extract_usage_data()
    assert usage.get("cached_tokens", 0) == 0


def test_openrouter_surfaces_cached_tokens():
    c = _bare(OpenRouterClient)
    details = types.SimpleNamespace(cached_tokens=256)
    usage = types.SimpleNamespace(prompt_tokens=800, completion_tokens=40, total_tokens=840,
                                  prompt_tokens_details=details)
    c.last_response = types.SimpleNamespace(usage=usage)
    out = c._extract_usage_data()
    assert out["cached_tokens"] == 256


def test_openrouter_no_details_safe():
    c = _bare(OpenRouterClient)
    usage = types.SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    c.last_response = types.SimpleNamespace(usage=usage)
    out = c._extract_usage_data()
    assert out.get("cached_tokens", 0) in (0, None)


def test_gemini_surfaces_cached_content_token_count():
    c = _bare(GeminiClient)
    meta = types.SimpleNamespace(prompt_token_count=2000, candidates_token_count=80,
                                 total_token_count=2080, cached_content_token_count=1024)
    c.last_response = types.SimpleNamespace(usage_metadata=meta)
    out = c._extract_usage_data()
    assert out["cached_tokens"] == 1024
