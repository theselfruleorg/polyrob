"""H4: every Anthropic registry entry set thinking_budget_tokens == max_completion_tokens.
When THINKING_CONFIG_ENABLED, the adapter did max_tokens = budget + 4096, which exceeds
the model's server-enforced completion cap -> 400 on every Claude call. _clamp_thinking
must keep budget < max_tokens <= cap (shrinking the budget when needed, disabling thinking
if the cap is too small to fit any).
"""
from modules.llm.anthropic_client import _clamp_thinking


def test_budget_equal_to_cap_is_clamped_below_cap():
    budget, max_tokens = _clamp_thinking(model_cap=64000, budget=64000, current_max_tokens=16384)
    assert budget is not None
    assert budget < 64000
    assert budget < max_tokens <= 64000


def test_normal_budget_bumps_max_tokens_above_budget_within_cap():
    budget, max_tokens = _clamp_thinking(model_cap=64000, budget=32000, current_max_tokens=16384)
    assert budget == 32000
    assert 32000 < max_tokens <= 64000


def test_max_tokens_already_above_budget_is_kept_but_capped():
    budget, max_tokens = _clamp_thinking(model_cap=64000, budget=8000, current_max_tokens=16384)
    assert budget == 8000
    assert max_tokens == 16384  # already > budget and <= cap


def test_tiny_cap_disables_thinking():
    budget, max_tokens = _clamp_thinking(model_cap=4000, budget=4000, current_max_tokens=2000)
    assert budget is None
    assert max_tokens <= 4000


def test_zero_budget_disables():
    assert _clamp_thinking(model_cap=64000, budget=0, current_max_tokens=16384) == (None, 16384)
