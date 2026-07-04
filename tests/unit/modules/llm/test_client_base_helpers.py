"""TDD tests for Task 2.4: base-class supports_vision fallback + _resolve_profile_base_url.

RED phase: these tests must fail until the helpers are added to LLMClient.

Summary of what we're testing:
A) LLMClient._resolve_supports_vision(model_type) -> bool
   - model NOT in registry → returns True (the common fallback across 4 identical blocks)
   - model IN registry with known vision flag → returns that flag value

B) LLMClient._resolve_profile_base_url(provider) -> Optional[str]
   - provider WITH a profile that has a base_url → returns it
   - provider with base_url=None in profile → returns None
   - unknown provider → returns None
"""

import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers to build minimal stub instances of LLMClient without needing real
# config/container setup (mirrors the pattern in test_adjust_max_tokens.py).
# ---------------------------------------------------------------------------

def _make_stub():
    """Instantiate a minimal concrete LLMClient subclass for testing."""
    from modules.llm.llm_client import LLMClient

    class _Stub(LLMClient):
        _skip_validate = True

        def __init__(self):
            # Bypass BaseModule / config entirely.
            self.model_type = "stub-model"
            self.max_tokens = 8000
            self.temperature = 0.7
            self.logger = MagicMock()
            self._initialized = False
            self._lock = MagicMock()
            self.config = MagicMock()
            self.config.get_llm_config.return_value = {}
            self.config.get.return_value = None
            self.name = "stub"
            self.container = None
            self._retries = 0
            self._max_retries = 3
            self._retry_delay = 1
            self.api_key = None
            self.cache_strategy = "none"

        # -- abstract stubs (not under test here) --
        async def generate_response(self, *a, **kw): pass
        async def _setup_client(self): pass
        async def _validate_connection(self): pass
        async def _cleanup_client(self): pass
        async def _generate(self, *a, **kw): pass
        async def _make_validation_request(self): pass
        def _check_validation_response(self, r): pass
        def _validate_config(self): pass  # skip real config check

    return _Stub()


# ===========================================================================
# A) supports_vision fallback
# ===========================================================================

class TestSupportsVisionFallback:
    """The base helper _resolve_supports_vision(model_type) must exist."""

    def test_model_not_in_registry_returns_true(self):
        """Unknown model → assume vision supported (matches 4 provider defaults)."""
        stub = _make_stub()
        with patch(
            "modules.llm.llm_client.get_model_config", return_value=None
        ):
            result = stub._resolve_supports_vision("some-unknown-model-xyz")
        assert result is True, (
            "When the model is not in the registry, _resolve_supports_vision "
            "must return True (the common provider fallback)."
        )

    def test_model_in_registry_vision_true(self):
        """Registry says vision=True → returns True."""
        stub = _make_stub()
        fake_config = MagicMock()
        fake_config.capabilities.supports_vision = True
        with patch(
            "modules.llm.llm_client.get_model_config", return_value=fake_config
        ):
            result = stub._resolve_supports_vision("gpt-5")
        assert result is True

    def test_model_in_registry_vision_false(self):
        """Registry says vision=False → returns False."""
        stub = _make_stub()
        fake_config = MagicMock()
        fake_config.capabilities.supports_vision = False
        with patch(
            "modules.llm.llm_client.get_model_config", return_value=fake_config
        ):
            result = stub._resolve_supports_vision("deepseek-chat")
        assert result is False


# ===========================================================================
# B) _resolve_profile_base_url
# ===========================================================================

class TestResolveProfileBaseUrl:
    """The base helper _resolve_profile_base_url(provider) must exist."""

    def test_provider_with_base_url_returns_it(self):
        """Provider that has a profile base_url → method returns it."""
        from modules.llm.profiles import get_profile

        stub = _make_stub()
        # "openrouter" has base_url set in PROFILES
        expected = get_profile("openrouter").base_url
        assert expected  # sanity: openrouter has a base_url in the fixture
        result = stub._resolve_profile_base_url("openrouter")
        assert result == expected

    def test_provider_with_none_base_url_returns_none(self):
        """Provider profile exists but base_url is None → method returns None."""
        stub = _make_stub()
        fake_profile = MagicMock()
        fake_profile.base_url = None
        with patch(
            "modules.llm.llm_client.get_profile", return_value=fake_profile
        ):
            result = stub._resolve_profile_base_url("openai")
        assert result is None

    def test_unknown_provider_returns_none(self):
        """No profile for provider → returns None."""
        stub = _make_stub()
        with patch(
            "modules.llm.llm_client.get_profile", return_value=None
        ):
            result = stub._resolve_profile_base_url("no-such-provider")
        assert result is None

    def test_profile_exception_returns_none(self):
        """If get_profile raises, _resolve_profile_base_url returns None (fail-open)."""
        stub = _make_stub()
        with patch(
            "modules.llm.llm_client.get_profile", side_effect=RuntimeError("boom")
        ):
            result = stub._resolve_profile_base_url("anthropic")
        assert result is None

    def test_anthropic_provider_returns_profile_base_url(self):
        """anthropic profile has a base_url — delegates correctly."""
        from modules.llm.profiles import get_profile

        stub = _make_stub()
        expected = get_profile("anthropic").base_url  # "https://api.anthropic.com"
        assert expected
        result = stub._resolve_profile_base_url("anthropic")
        assert result == expected

    def test_nvidia_provider_returns_profile_base_url(self):
        """nvidia profile has a base_url — delegates correctly."""
        from modules.llm.profiles import get_profile

        stub = _make_stub()
        expected = get_profile("nvidia").base_url
        assert expected
        result = stub._resolve_profile_base_url("nvidia")
        assert result == expected
