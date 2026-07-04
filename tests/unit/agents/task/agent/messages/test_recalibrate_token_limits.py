"""B6 (high) — recalibrate_token_counts must preserve the completion reserve and
keep safe_input_tokens consistent after a model fallback to a smaller window.

The old code set max_input_tokens = raw context_window (no completion reserve, so
zero room for the response) and never updated safe_input_tokens (left stale-large,
so check_token_safety could report safe while the request overflows).
"""
import logging
from types import SimpleNamespace

from agents.task.agent.messages.token_counter import TokenCounterMixin
from agents.task.robust_parse_config import RobustParseConfig


class _Host(TokenCounterMixin):
    def __init__(self, context_window, completion, *, max_input, safe_input):
        self.logger = logging.getLogger("test.recal")
        self._model_name = "small-model"
        self.max_input_tokens = max_input
        self.safe_input_tokens = safe_input
        self.history = SimpleNamespace(messages=[], total_tokens=0)
        self._ctx, self._comp = context_window, completion

    @property
    def model_name(self):
        return self._model_name

    def _get_model_token_limits(self):
        return self._ctx, self._comp


def test_recalibrate_preserves_completion_reserve_and_updates_safe():
    # Simulate a fallback from a big-context model (leftover large limits) to a
    # 128k model.
    host = _Host(128000, 16384, max_input=900000, safe_input=855000)
    host.recalibrate_token_counts(force=True)

    expected_max = max(1000, int(128000 * 0.95) - 16384)
    assert host.max_input_tokens == expected_max
    assert host.max_input_tokens < 128000  # reserve kept, NOT the raw window
    # safe_input updated and strictly below max (no longer stale-large)
    assert host.safe_input_tokens == int(expected_max * (1 - RobustParseConfig.SAFETY_MARGIN_PERCENT))
    assert host.safe_input_tokens < host.max_input_tokens


def test_recalibrate_does_not_inflate_for_larger_model():
    # New computed budget >= current => leave limits untouched (only shrink).
    host = _Host(128000, 16384, max_input=50000, safe_input=47500)
    host.recalibrate_token_counts(force=True)
    assert host.max_input_tokens == 50000
    assert host.safe_input_tokens == 47500
