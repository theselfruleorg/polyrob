"""2026-08-17: session-level vision gate (backlog item 1 of the provider-outage
handoff).

use_vision=True with a non-vision model put screenshots into HISTORY; the
runner's per-call strip only covers get_next_action, so every OTHER call site
(output validation, compaction, H-MEM writes) shipped them to the adapter —
~1.8k "[IMAGE]"-replacement warnings in 4 days of prod journal (glm-5 alone:
1,118). The gate resolves once at construction from the same registry-backed
check the runner uses.
"""
import logging

from agents.task.agent.core.construction import _resolve_session_vision


class _FakeAgent:
    def __init__(self, supports_vision, model_name="glm-5"):
        self._supports = supports_vision
        self._model_name = model_name
        self.logger = logging.getLogger("test-session-vision-gate")

    def _extract_model_name(self, llm):
        return self._model_name

    def _check_vision_support(self, model_name):
        return self._supports


def test_gate_disables_for_non_vision_model():
    assert _resolve_session_vision(_FakeAgent(False), True, object()) is False


def test_gate_keeps_vision_for_capable_model():
    assert _resolve_session_vision(_FakeAgent(True), True, object()) is True


def test_gate_respects_config_off():
    assert _resolve_session_vision(_FakeAgent(True), False, object()) is False


def test_gate_fails_open_on_probe_error():
    """A capability-probe fault must never break construction — keep the
    configured value; the runner's per-call strip remains the backstop."""
    agent = _FakeAgent(True)

    def _boom(llm):
        raise RuntimeError("no registry")

    agent._extract_model_name = _boom
    assert _resolve_session_vision(agent, True, object()) is True
