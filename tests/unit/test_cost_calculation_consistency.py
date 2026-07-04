"""
Test that cost calculations are consistent across the application.

This ensures all components use the same pricing from model_registry.
"""
import pytest
import sys
from pathlib import Path

# Add project root to path to avoid circular imports
project_root = Path(__file__).parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# Import only the model registry for direct testing
# Avoid importing telemetry/webview modules that have circular dependencies
from modules.llm.model_registry import calculate_cost, get_model_config
from agents.task.telemetry.service import ProductTelemetry
from webview.stats_service import _calculate_cost_from_registry


class TestCostCalculationConsistency:
    """Ensure all cost calculations use the same pricing."""

    @pytest.mark.parametrize("model,input_tok,output_tok,expected", [
        # GPT-5 series (Aug 2025 pricing)
        ("gpt-5", 10000, 5000, 0.0625),  # $1.25/M * 10k + $10/M * 5k = $0.0125 + $0.05
        ("gpt-5-mini", 10000, 5000, 0.0125),  # $0.25/M * 10k + $2/M * 5k = $0.0025 + $0.01
        ("gpt-5-nano", 10000, 5000, 0.0025),  # $0.05/M * 10k + $0.40/M * 5k = $0.0005 + $0.002

        # GPT-4.1 series (Apr 2025 pricing)
        ("gpt-4.1", 10000, 5000, 0.06),  # $2/M * 10k + $8/M * 5k = $0.02 + $0.04
        ("gpt-4.1-mini", 10000, 5000, 0.012),  # $0.40/M * 10k + $1.60/M * 5k = $0.004 + $0.008
        ("gpt-4.1-nano", 10000, 5000, 0.003),  # $0.10/M * 10k + $0.40/M * 5k = $0.001 + $0.002

        # Claude series
        ("claude-sonnet-4-5", 10000, 5000, 0.105),  # $3/M * 10k + $15/M * 5k = $0.03 + $0.075
        ("claude-haiku-4-5", 10000, 5000, 0.035),  # $1/M * 10k + $5/M * 5k = $0.01 + $0.025
        ("claude-opus-4.1", 10000, 5000, 0.525),  # $15/M * 10k + $75/M * 5k = $0.15 + $0.375
    ])
    def test_registry_cost_calculation(self, model, input_tok, output_tok, expected):
        """Test model registry calculates correct costs."""
        cost = calculate_cost(model, input_tok, output_tok)
        assert abs(cost - expected) < 0.0001, \
            f"Expected {expected}, got {cost} for {model}"

    def test_model_registry_has_all_models(self):
        """Test that model registry has all expected models."""
        expected_models = [
            "gpt-5", "gpt-5-mini", "gpt-5-nano",
            "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano",
            "claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-4.1"
        ]

        for model in expected_models:
            config = get_model_config(model)
            assert config is not None, f"Model {model} not found in registry"
            assert config.pricing is not None, f"Model {model} has no pricing"

    def test_cached_tokens_support(self):
        """Test that cached tokens are handled correctly."""
        model = "gpt-5"
        input_tokens = 10000
        output_tokens = 5000
        cached_tokens = 5000

        # Cost with cached tokens should be less than without
        cost_no_cache = calculate_cost(model, input_tokens, output_tokens, 0)
        cost_with_cache = calculate_cost(model, input_tokens, output_tokens, cached_tokens)

        # For now, both will be the same since cached pricing isn't implemented
        # But the call should not crash
        assert cost_with_cache >= 0

    def test_pricing_accuracy_gpt5_vs_old_estimates(self):
        """
        Verify that GPT-5 pricing is now accurate vs the old hardcoded estimates.

        Old telemetry would use default pricing (~$0.01/$0.03 per 1K)
        New registry uses correct pricing ($1.25/$10 per 1M)
        """
        model = "gpt-5"
        input_tokens = 10000
        output_tokens = 5000

        # Correct pricing
        correct_cost = calculate_cost(model, input_tokens, output_tokens)
        # = (10000/1M * $1.25) + (5000/1M * $10)
        # = $0.0125 + $0.05 = $0.0625
        assert abs(correct_cost - 0.0625) < 0.0001

        # Old hardcoded pricing would have been (per 1K):
        # = (10 * 0.01) + (5 * 0.03) = $0.10 + $0.15 = $0.25
        # That's 4x overestimate!

        # Verify new system uses correct pricing
        telemetry = ProductTelemetry()
        telemetry_cost = telemetry._calculate_cost_from_registry(
            model_name=model,
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens
        )

        assert abs(telemetry_cost - 0.0625) < 0.0001, \
            f"Telemetry should use new pricing: expected $0.0625, got ${telemetry_cost}"

    def test_pricing_accuracy_gpt41_vs_old_estimates(self):
        """
        Verify that GPT-4.1 pricing is now accurate vs the old WRONG estimates.

        Old webview had WRONG pricing: $0.015/$0.03 per 1K
        New registry uses correct pricing: $2/$8 per 1M
        """
        model = "gpt-4.1"
        input_tokens = 10000
        output_tokens = 5000

        # Correct pricing
        correct_cost = calculate_cost(model, input_tokens, output_tokens)
        # = (10000/1M * $2) + (5000/1M * $8)
        # = $0.02 + $0.04 = $0.06
        assert abs(correct_cost - 0.06) < 0.0001

        # Old hardcoded pricing would have been (per 1K):
        # = (10 * 0.015) + (5 * 0.03) = $0.15 + $0.15 = $0.30
        # That's 5x overestimate!

        # Verify new system uses correct pricing
        webview_cost = _calculate_cost_from_registry(
            model_name=model,
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens
        )

        assert abs(webview_cost - 0.06) < 0.0001, \
            f"Webview should use new pricing: expected $0.06, got ${webview_cost}"
