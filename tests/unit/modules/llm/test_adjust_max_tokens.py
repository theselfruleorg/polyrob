"""TDD tests for LLMClient._adjust_max_tokens base method (Task 2.1).

RED phase: these tests must fail until _adjust_max_tokens is added to LLMClient.
"""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock


# ---------------------------------------------------------------------------
# Minimal concrete subclass for testing (no real client setup needed)
# ---------------------------------------------------------------------------

class _StubLLMClient:
    """Stub that mimics just the LLMClient surface we need for testing."""

    def __init__(self, context_window: int, max_completion: int, max_tokens: int = 8000):
        self.model_type = "stub-model"
        self.max_tokens = max_tokens
        self.logger = MagicMock()

    def get_context_window(self) -> int:
        return self._context_window

    def get_max_completion_tokens(self) -> int:
        return self._max_completion


def _make_stub(context_window: int, max_completion: int, max_tokens: int = 8000):
    """Build a stub client with known limits, importing the real base."""
    from modules.llm.llm_client import LLMClient

    class ConcreteStub(LLMClient):
        """Minimal concrete subclass — all abstract methods are no-ops."""

        _skip_validate = True  # avoid _validate_config touching real config

        def __init__(self):
            # Bypass BaseModule __init__ entirely; set attrs manually.
            self.model_type = "stub-model"
            self.max_tokens = max_tokens
            self.logger = MagicMock()
            self._context_window = context_window
            self._max_completion = max_completion

        def get_context_window(self) -> int:
            return self._context_window

        def get_max_completion_tokens(self) -> int:
            return self._max_completion

        # --- Abstract method stubs ---
        async def _setup_client(self): pass
        async def _validate_connection(self): pass
        async def _cleanup_client(self): pass
        async def _generate(self, *a, **kw): pass
        async def generate_response(self, *a, **kw): return ""
        async def _make_validation_request(self): return None
        def _check_validation_response(self, r): pass

    return ConcreteStub()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MESSAGES_5_TOKENS = [{"role": "user", "content": "hello world this is test message"}]


def _call(stub, requested: int, estimated_input: int):
    """
    Call _adjust_max_tokens with a pre-computed input token count.
    We use estimated_input_tokens= keyword so callers that pre-compute
    system+messages tokens can pass them in (the Anthropic/Gemini pattern).
    """
    return stub._adjust_max_tokens(
        messages=MESSAGES_5_TOKENS,
        max_tokens=requested,
        estimated_input_tokens=estimated_input,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAdjustMaxTokensBase:
    """Tests for LLMClient._adjust_max_tokens (must exist on base)."""

    def test_method_exists_on_base(self):
        """The base LLMClient must expose _adjust_max_tokens."""
        from modules.llm.llm_client import LLMClient
        assert hasattr(LLMClient, "_adjust_max_tokens"), (
            "_adjust_max_tokens not found on LLMClient base class"
        )

    def test_requested_below_all_caps_unchanged(self):
        """When requested < completion_limit and fits context, return requested."""
        stub = _make_stub(context_window=32000, max_completion=8192)
        # requested=2000, input=1000 → remaining=32000-1000-100=30900 > 2000 < 8192
        result = _call(stub, requested=2000, estimated_input=1000)
        assert result == 2000

    def test_clamped_to_completion_limit(self):
        """When requested > max_completion_tokens, clamp to completion limit."""
        stub = _make_stub(context_window=32000, max_completion=4096)
        # requested=8000 > 4096 → clamped to 4096 (still fits in context)
        result = _call(stub, requested=8000, estimated_input=100)
        assert result == 4096

    def test_clamped_to_context_window(self):
        """When input tokens leave little room, clamp to remaining context."""
        stub = _make_stub(context_window=4000, max_completion=8192)
        # input=3500 → remaining = 4000-3500-100 = 400 < requested=2000
        result = _call(stub, requested=2000, estimated_input=3500)
        assert result == 400

    def test_floor_at_one(self):
        """When remaining context is <= 0, return at least 1."""
        stub = _make_stub(context_window=1000, max_completion=8192)
        # input=1000 → remaining = 1000-1000-100 = -100 → max(1, -100) = 1
        result = _call(stub, requested=500, estimated_input=1000)
        assert result == 1

    def test_both_limits_apply_completion_wins(self):
        """When both limits apply, the tighter one wins (completion < context-remaining)."""
        stub = _make_stub(context_window=10000, max_completion=200)
        # completion=200 < context-remaining=10000-100-100=9800, so completion wins
        result = _call(stub, requested=5000, estimated_input=100)
        assert result == 200

    def test_both_limits_apply_context_wins(self):
        """When context-remaining < completion limit, context-remaining wins."""
        stub = _make_stub(context_window=1500, max_completion=8192)
        # context-remaining=1500-1000-100=400 < 8192 → 400 wins
        result = _call(stub, requested=8192, estimated_input=1000)
        assert result == 400

    def test_uses_self_max_tokens_when_none(self):
        """When max_tokens=None, fall back to self.max_tokens."""
        stub = _make_stub(context_window=32000, max_completion=8192, max_tokens=1000)
        # self.max_tokens=1000, fits both limits → return 1000
        result = stub._adjust_max_tokens(
            messages=MESSAGES_5_TOKENS,
            max_tokens=None,
            estimated_input_tokens=100,
        )
        assert result == 1000

    def test_messages_token_count_used_when_no_precomputed(self):
        """When estimated_input_tokens not provided, count from messages."""
        stub = _make_stub(context_window=32000, max_completion=8192)
        # Just check it doesn't raise and returns something positive
        result = stub._adjust_max_tokens(messages=MESSAGES_5_TOKENS, max_tokens=1000)
        assert result > 0


class TestAdjustMaxTokensDeepSeekInherits:
    """DeepSeek no longer defines its own _adjust_max_tokens — it inherits base."""

    def test_deepseek_no_own_method(self):
        """DeepSeek should NOT define _adjust_max_tokens in its own __dict__."""
        from modules.llm.deepseek_client import DeepSeekClient
        assert "_adjust_max_tokens" not in DeepSeekClient.__dict__, (
            "DeepSeekClient still has its own _adjust_max_tokens — should be deleted"
        )


class TestAdjustMaxTokensOpenRouterInherits:
    """OpenRouter no longer defines its own _adjust_max_tokens — it inherits base."""

    def test_openrouter_no_own_method(self):
        """OpenRouter should NOT define _adjust_max_tokens in its own __dict__."""
        from modules.llm.openrouter_client import OpenRouterClient
        assert "_adjust_max_tokens" not in OpenRouterClient.__dict__, (
            "OpenRouterClient still has its own _adjust_max_tokens — should be deleted"
        )


# ---------------------------------------------------------------------------
# Absolute output cap (live-test F6): credit-metered providers (OpenRouter)
# pre-authorize against max_tokens, so requesting a model's full completion
# ceiling (e.g. GLM 262144) causes spurious 402s + cost waste. _adjust_max_tokens
# must clamp to an absolute, env-overridable per-request output cap.
# ---------------------------------------------------------------------------

def test_absolute_output_cap_caps_huge_request(monkeypatch):
    monkeypatch.delenv("LLM_MAX_OUTPUT_TOKENS", raising=False)
    stub = _make_stub(context_window=1_048_576, max_completion=262_144)
    # huge request, tiny input, huge context → only the absolute default cap binds
    assert _call(stub, requested=262_144, estimated_input=100) == 16_384


def test_absolute_output_cap_env_override(monkeypatch):
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "4096")
    stub = _make_stub(context_window=1_048_576, max_completion=262_144)
    assert _call(stub, requested=262_144, estimated_input=100) == 4096


def test_absolute_output_cap_does_not_raise_small_requests(monkeypatch):
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "16384")
    stub = _make_stub(context_window=1_048_576, max_completion=262_144)
    # a request already under the cap is unchanged (cap is a ceiling, not a floor)
    assert _call(stub, requested=2000, estimated_input=100) == 2000
