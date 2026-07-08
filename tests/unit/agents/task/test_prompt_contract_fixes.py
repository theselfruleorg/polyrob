"""Prompt-contract fixes from the 2026-07-06 structural review.

T1-02: the brain-state JSON example rendered with doubled braces ({{ ... }}) — an
f-string value that never gets a second .format() pass, so the model was shown malformed
pseudo-JSON.
T1-04: <rules> threatened "VIOLATION = REJECTION. After 3 failures, session halts" for
the tool-free planning turn the runtime deliberately allows, and cited the wrong halt
threshold.
T1-05: nothing told the agent to act with judgment — added a static <agency> section.
"""
import pytest

from agents.task.agent.prompts import SystemPrompt


def _render(native: bool) -> str:
    sp = SystemPrompt(action_description="- done(text): finish the task", use_native_tools=native)
    return sp.get_system_message().content


@pytest.mark.parametrize("native", [True, False])
def test_no_doubled_open_brace_in_brain_state_example(native):
    # doubled-OPEN braces are the bug artifact; adjacent CLOSING }} is valid nested JSON
    c = _render(native)
    assert "{{" not in c
    assert '"current_state": {' in c  # valid single-brace object open


@pytest.mark.parametrize("native", [True, False])
def test_rules_state_real_contract_not_false_threat(native):
    c = _render(native)
    assert "VIOLATION = REJECTION" not in c
    assert "After 3 failures" not in c
    # the real contract: a planning turn is allowed, and the real halt threshold is cited
    assert "planning" in c.lower()
    from agents.task.constants import DEFAULT_MAX_FAILURES
    assert f"{DEFAULT_MAX_FAILURES} consecutive failures" in c


@pytest.mark.parametrize("native", [True, False])
def test_agency_section_present(native):
    c = _render(native)
    assert "<agency>" in c
    assert "DECIDE and ACT" in c
    assert "verify the" in c  # verify-before-done guidance


# ---- T1-12 / T1-14 / T1-15 (same review, second pass) ----------------------

def _section(content: str, tag: str) -> str:
    assert f"<{tag}>" in content, f"missing <{tag}> section"
    return content.split(f"<{tag}>")[1].split(f"</{tag}>")[0]


def test_delegation_section_teaches_without_fear(monkeypatch):
    """T1-12: the old section was 454 tokens of deterrence (WARNING/DO NOT/cost
    scare) that discouraged the parallelism the platform ships. It must teach the
    verb and the honest trade-off without fear framing."""
    monkeypatch.setenv("SUB_AGENTS_ENABLED", "true")
    c = _render(True)
    sec = _section(c, "subtask-delegation")
    assert "WARNING" not in sec
    assert "DO NOT" not in sec
    assert "EXPENSIVE" not in sec.upper() or "EXPENSIVE" not in sec  # no shouting
    assert "delegate_task" in sec
    assert "independent" in sec  # still teaches when it IS the right tool
    assert "brief" in sec.lower() or "self-contained" in sec.lower()


def test_communication_states_real_reply_exit_contract():
    """T1-15: the prompt claimed a non-blocking send never ends the turn, while
    the runtime ends it after N consecutive reply-only steps."""
    from agents.task.agent.core.conversational_exit import (
        CONVERSATIONAL_EXIT_AFTER_REPLIES,
    )

    c = _render(True)
    comm = _section(c, "communication")
    assert "does NOT end your turn" not in comm
    assert str(CONVERSATIONAL_EXIT_AFTER_REPLIES) in comm
    assert "reply-only" in comm


def test_memory_prose_has_no_hardcoded_cadence():
    """T1-14: '10 message exchanges' / 'every 10 steps' were prose constants not
    derived from config — stale the moment either knob moves."""
    c = _render(True)
    mem = _section(c, "memory-system")
    assert "Last 10 message" not in mem
    assert "every 10 steps" not in mem
