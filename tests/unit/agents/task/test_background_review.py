"""W2-C — background-review fork: cadence decision + gating + sub-agent exemption."""
import types

import pytest

from agents.task.agent.core.background_review import BackgroundReviewMixin


class _Host(BackgroundReviewMixin):
    """Minimal host exposing just what _bg_review_should_fire reads."""
    def __init__(self, is_sub=False):
        self._is_sub_agent = is_sub
        self._bg_review_productive_turns = 0


def test_disabled_never_fires(monkeypatch):
    monkeypatch.setenv("BACKGROUND_REVIEW_ENABLED", "false")
    h = _Host()
    assert all(not h._bg_review_should_fire(True) for _ in range(20))


def test_fires_every_interval(monkeypatch):
    monkeypatch.setenv("BACKGROUND_REVIEW_ENABLED", "true")
    monkeypatch.setenv("BG_REVIEW_INTERVAL", "3")
    monkeypatch.setenv("SKILLS_WRITABLE", "true")
    h = _Host()
    fires = [h._bg_review_should_fire(True) for _ in range(7)]
    # fire on the 3rd and 6th productive turns
    assert fires == [False, False, True, False, False, True, False]


def test_unproductive_turns_dont_count(monkeypatch):
    monkeypatch.setenv("BACKGROUND_REVIEW_ENABLED", "true")
    monkeypatch.setenv("BG_REVIEW_INTERVAL", "2")
    monkeypatch.setenv("SKILLS_WRITABLE", "true")
    h = _Host()
    assert h._bg_review_should_fire(False) is False
    assert h._bg_review_should_fire(False) is False
    assert h._bg_review_should_fire(True) is False  # 1st productive
    assert h._bg_review_should_fire(True) is True   # 2nd productive -> fire


def test_sub_agents_exempt(monkeypatch):
    monkeypatch.setenv("BACKGROUND_REVIEW_ENABLED", "true")
    monkeypatch.setenv("BG_REVIEW_INTERVAL", "1")
    monkeypatch.setenv("SKILLS_WRITABLE", "true")
    h = _Host(is_sub=True)
    assert h._bg_review_should_fire(True) is False  # a reviewer never forks a reviewer


def test_spawn_is_fail_open(monkeypatch):
    monkeypatch.setenv("BACKGROUND_REVIEW_ENABLED", "true")
    monkeypatch.setenv("BG_REVIEW_INTERVAL", "1")
    monkeypatch.setenv("SKILLS_WRITABLE", "true")
    h = _Host()
    h.orchestrator = types.SimpleNamespace(session_id="s")
    # no event loop running create_task would normally raise; the method must swallow it
    h._maybe_spawn_background_review(turn_was_productive=True)  # must not raise


def test_review_prompt_excludes_self_context_when_flag_off(monkeypatch):
    # polyrob Phase E: the reviewer only proposes SELF-context refinements when
    # SELF_CONTEXT_WRITABLE is on. Off => skills-only prompt (byte-identical legacy).
    monkeypatch.setenv("SELF_CONTEXT_WRITABLE", "false")
    from agents.task.agent.core.background_review import build_review_prompt
    p = build_review_prompt()
    assert "self_context_manage" not in p
    assert "skill_manage" in p


def test_review_prompt_includes_self_context_when_flag_on(monkeypatch):
    monkeypatch.setenv("SELF_CONTEXT_WRITABLE", "true")
    from agents.task.agent.core.background_review import build_review_prompt
    p = build_review_prompt()
    assert "self_context_manage" in p
    # it must frame the proposal as quarantined/consolidated, not auto-applied
    assert "quarantin" in p.lower() or "review" in p.lower()
