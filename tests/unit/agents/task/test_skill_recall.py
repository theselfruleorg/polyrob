"""P2-1b — recall fixes: synonym normalization + miss logging.

The substring matcher is lossy ("slideshow" doesn't contain "slides"). A tiny curated
synonym map closes the worst gaps; a miss log makes zero-match sessions observable.
"""
import logging

from agents.task.agent.skill_manager import SkillManager


def test_slideshow_matches_slides_via_synonym():
    sm = SkillManager()
    matched = {m.skill_id for m in sm.get_skills_for_session(task="build a slideshow", max_skills=20)}
    assert "presentation-creator" in matched


def test_zero_match_logs_available_ids(caplog):
    sm = SkillManager()
    with caplog.at_level(logging.INFO, logger="agents.task.agent.skill_manager"):
        sm.get_skills_for_session(task="zxqw nonsense gibberish unmatched", max_skills=20)
    # The miss is observable: some INFO record mentions skills were available but unmatched.
    assert any("no skills matched" in r.message.lower() for r in caplog.records)
