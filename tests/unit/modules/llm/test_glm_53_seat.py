"""GLM-5.3 / GLM-5.3-Flash on the z.ai GLM Coding Plan seat (2026-09-15).

Live-probed against ``api.z.ai/api/anthropic`` with the prod key before this was
written: both ids answer 200, both call tools natively, both cap ``max_tokens`` at
131072, and ``glm-5.3-flash`` READS AN IMAGE while ``glm-5.3`` does not.

These pins exist because the failure they guard is silent: an unregistered model id
falls back to ``z-ai/glm-5.2``'s metadata, so a wrong context window or a wrong
vision answer looks exactly like a right one.
"""
import pytest

from modules.llm.model_registry import get_model_config_exact
from modules.llm.provider_spec import get_spec


class TestCatalogRows:
    def test_glm_53_registered_with_probed_limits(self):
        cfg = get_model_config_exact("glm-5.3")
        assert cfg is not None, "bare 'glm-5.3' must resolve (it is the id the seat serves)"
        assert cfg.name == "z-ai/glm-5.3"
        assert cfg.context_window == 1310720
        # z.ai rejects max_tokens>131072 with code 1210 on this endpoint.
        assert cfg.max_completion_tokens == 131072
        assert cfg.capabilities.supports_tools

    def test_glm_53_is_text_only(self):
        # Given a base64 PNG it narrated a URL it could not fetch. Claiming vision
        # here would licence a hallucination, not add a capability.
        assert get_model_config_exact("glm-5.3").capabilities.supports_vision is False

    def test_flash_registered_and_is_the_vision_model(self):
        cfg = get_model_config_exact("glm-5.3-flash")
        assert cfg is not None
        assert cfg.name == "z-ai/glm-5.3-flash"
        assert cfg.context_window == 1310720
        assert cfg.max_completion_tokens == 131072
        assert cfg.capabilities.supports_tools
        # The ONE multimodal model a GLM-Coding-Plan deployment has.
        assert cfg.capabilities.supports_vision is True

    def test_flash_is_the_cheap_tier(self):
        flash = get_model_config_exact("glm-5.3-flash").pricing
        flagship = get_model_config_exact("glm-5.3").pricing
        assert flash.input_price < flagship.input_price
        assert flash.output_price < flagship.output_price


class TestSeatSpec:
    def test_seat_serves_both_and_defaults_to_the_flagship(self):
        spec = get_spec("zai-coding")
        assert spec.default_model == "glm-5.3"
        assert "glm-5.3" in spec.models
        assert "glm-5.3-flash" in spec.models

    def test_older_pins_still_resolve(self):
        # A job payload or an env pin may still name these; dropping them would
        # strand durable rows written before this change.
        spec = get_spec("zai-coding")
        assert "glm-5" in spec.models
        assert "glm-5.2" in spec.models
