"""P1 (context-usage audit 2026-08-15) — the input-budget formula must cap the
completion reserve instead of subtracting the model's FULL max_completion_tokens.

Root cause of the "hey = ctx 43%" incident: `z-ai/glm-5` pins
max_completion_tokens=131072 on a 202752 window, so the legacy formula
(0.95*window - reserve) shrank the input budget to 61,542 (65% of the window
reserved for output). Five registry rows with reserve==window clamped all the
way to the 1,000-token floor. The fix: reserve = min(max_completion_tokens,
COMPLETION_RESERVE_TOKENS) with default 16384; `COMPLETION_RESERVE_TOKENS=0`
restores the legacy full-reserve behavior.
"""
import logging
from types import SimpleNamespace

import pytest

import agents.task.agent.messages.token_counter as tc_mod
from agents.task.agent.messages.token_counter import TokenCounterMixin
from agents.task.robust_parse_config import RobustParseConfig


class _CalcHost(TokenCounterMixin):
    """Minimal host for _calculate_token_limits."""

    def __init__(self, model_name="test-model"):
        self.logger = logging.getLogger("test.reserve_cap")
        self._model_name = model_name

    @property
    def model_name(self):
        return self._model_name


class _RecalHost(TokenCounterMixin):
    """Minimal host for recalibrate_token_counts (mirrors test_recalibrate_token_limits)."""

    def __init__(self, context_window, completion, *, max_input, safe_input):
        self.logger = logging.getLogger("test.reserve_cap.recal")
        self._model_name = "test-model"
        self.max_input_tokens = max_input
        self.safe_input_tokens = safe_input
        self.history = SimpleNamespace(messages=[], total_tokens=0)
        self._ctx, self._comp = context_window, completion

    @property
    def model_name(self):
        return self._model_name

    def _get_model_token_limits(self):
        return self._ctx, self._comp


def _patch_model(monkeypatch, context_window, max_completion):
    cfg = SimpleNamespace(context_window=context_window,
                          max_completion_tokens=max_completion)
    import modules.llm.model_registry as reg
    monkeypatch.setattr(reg, "get_model_config", lambda name: cfg)


def _calc(monkeypatch, window, completion):
    _patch_model(monkeypatch, window, completion)
    host = _CalcHost()
    return host._calculate_token_limits(llm=None, max_input_override=None)


def test_default_cap_restores_glm5_budget(monkeypatch):
    monkeypatch.delenv("COMPLETION_RESERVE_TOKENS", raising=False)
    max_input, safe_input, reserve = _calc(monkeypatch, 202752, 131072)
    assert reserve == 16384
    assert max_input == int(202752 * 0.95) - 16384  # 176,230 — not 61,542
    assert safe_input == int(max_input * (1 - RobustParseConfig.SAFETY_MARGIN_PERCENT))


def test_default_cap_rescues_reserve_equals_window_rows(monkeypatch):
    # kimi-k2 shape: reserve == window used to clamp the budget to the 1,000 floor.
    monkeypatch.delenv("COMPLETION_RESERVE_TOKENS", raising=False)
    max_input, _safe, reserve = _calc(monkeypatch, 131072, 131072)
    assert reserve == 16384
    assert max_input == int(131072 * 0.95) - 16384  # 108,134 — not 1,000


def test_small_reserve_unchanged_by_cap(monkeypatch):
    monkeypatch.delenv("COMPLETION_RESERVE_TOKENS", raising=False)
    max_input, _safe, reserve = _calc(monkeypatch, 128000, 8192)
    assert reserve == 8192
    assert max_input == int(128000 * 0.95) - 8192


def test_zero_disables_cap_legacy_behavior(monkeypatch):
    monkeypatch.setenv("COMPLETION_RESERVE_TOKENS", "0")
    max_input, _safe, reserve = _calc(monkeypatch, 202752, 131072)
    assert reserve == 131072
    assert max_input == int(202752 * 0.95) - 131072  # legacy 61,542


def test_explicit_env_cap_wins(monkeypatch):
    monkeypatch.setenv("COMPLETION_RESERVE_TOKENS", "32768")
    max_input, _safe, reserve = _calc(monkeypatch, 202752, 131072)
    assert reserve == 32768
    assert max_input == int(202752 * 0.95) - 32768


def test_garbage_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("COMPLETION_RESERVE_TOKENS", "not-a-number")
    _max, _safe, reserve = _calc(monkeypatch, 202752, 131072)
    assert reserve == 16384


def test_recalibrate_applies_the_same_cap(monkeypatch):
    monkeypatch.delenv("COMPLETION_RESERVE_TOKENS", raising=False)
    # Fallback onto a glm-5-shaped model: the recalibrated budget must use the
    # capped reserve, mirroring _calculate_token_limits.
    host = _RecalHost(202752, 131072, max_input=900000, safe_input=855000)
    host.recalibrate_token_counts(force=True)
    expected_max = int(202752 * 0.95) - 16384
    assert host.max_input_tokens == expected_max
    assert host.safe_input_tokens == int(
        expected_max * (1 - RobustParseConfig.SAFETY_MARGIN_PERCENT))


def test_recalibrate_zero_env_keeps_legacy(monkeypatch):
    monkeypatch.setenv("COMPLETION_RESERVE_TOKENS", "0")
    host = _RecalHost(202752, 131072, max_input=900000, safe_input=855000)
    host.recalibrate_token_counts(force=True)
    assert host.max_input_tokens == max(1000, int(202752 * 0.95) - 131072)
