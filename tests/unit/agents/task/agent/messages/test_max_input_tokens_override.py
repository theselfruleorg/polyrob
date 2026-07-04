"""TASK_MAX_INPUT_TOKENS env override for _calculate_token_limits.

Enables capping the effective input-token budget regardless of the model's native
context window — an operational knob (cost control / constrained deploys) and the
lever used to exercise compaction under load on large-context models. An explicit
constructor override still wins; unset env preserves auto-calc from the registry.
"""
import logging

from agents.task.agent.messages.token_counter import TokenCounterMixin
from agents.task.robust_parse_config import RobustParseConfig


class _Host(TokenCounterMixin):
    def __init__(self):
        self.logger = logging.getLogger("test.tokens")
        self._model_name = "x-ai/grok-4.3"


def test_env_override_caps_budget(monkeypatch):
    monkeypatch.setenv("TASK_MAX_INPUT_TOKENS", "10000")
    host = _Host()
    max_input, safe_input, _ = host._calculate_token_limits(None, None)
    assert max_input == 10000
    assert safe_input == int(10000 * (1 - RobustParseConfig.SAFETY_MARGIN_PERCENT))


def test_explicit_override_beats_env(monkeypatch):
    monkeypatch.setenv("TASK_MAX_INPUT_TOKENS", "10000")
    host = _Host()
    max_input, _, _ = host._calculate_token_limits(None, 50000)
    assert max_input == 50000


def test_unset_env_falls_through_to_auto(monkeypatch):
    monkeypatch.delenv("TASK_MAX_INPUT_TOKENS", raising=False)
    host = _Host()
    # Grok 4.3 has a large window — auto-calc must be far above any small cap.
    max_input, _, _ = host._calculate_token_limits(None, None)
    assert max_input > 100000
