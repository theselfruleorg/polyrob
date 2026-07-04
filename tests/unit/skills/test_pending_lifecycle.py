"""Task 10 (SK-F2, SK-F8): pending-draft lifecycle.

Two defects fixed here:

- SK-F2: a background-review-authored skill always lands in ``.pending/``
  (quarantined, awaiting owner review) — so its ``load_count`` can never rise.
  The curator's Phase-1 age/reuse sweep didn't know about this and archived
  never-reused authored skills purely by age, sweeping up drafts nobody has
  reviewed yet. Fixed: the curator skips any row whose resolved file lives
  under a ``.pending/`` path segment.
- SK-F8: the background reviewer's whole output rail is ``skill_manage``
  (create), so firing it while ``SKILLS_WRITABLE=false`` burns an aux-model
  run that is guaranteed to fail at the tool layer. Fixed: the cadence gate
  also checks ``AutonomyConfig.skills_writable()``.
"""
from pathlib import Path

import pytest

from agents.task.agent.core.background_review import BackgroundReviewMixin
from agents.task.agent.core.curator import SkillCurator
from agents.task.agent.skill_manager import SkillManager
from modules.skills.skill_usage import SkillUsageStore

DAY = 86400.0


# =====================================================================
# (a) SK-F2 — curator never archives a .pending/ draft
# =====================================================================

def test_curator_never_archives_pending_draft(tmp_path, monkeypatch):
    monkeypatch.setenv("CURATOR_STALE_DAYS", "30")
    monkeypatch.setenv("CURATOR_ARCHIVE_DAYS", "90")

    usage = SkillUsageStore(str(tmp_path / "skill_usage.db"), clock=lambda: 1_000_000.0)
    monkeypatch.setattr("modules.skills.skill_usage.get_skill_usage_store", lambda: usage)

    sm = SkillManager(skills_dir=tmp_path / "skills")
    result = sm.create_skill(
        "learned-thing",
        "# Learned Thing\n\nWhen to use it.\n\nSteps:\n1. Do the thing.\n",
        user_id="u1",
        created_by="background_review",
    )
    assert result.ok is True
    assert result.pending is True, "background_review authorship must always quarantine"
    pending_file = Path(result.path)
    assert ".pending" in pending_file.parts
    assert pending_file.exists()

    # Age the provenance row well past CURATOR_ARCHIVE_DAYS, never reused
    # (load_count == 0 forever — the row is uncatalogued so it can never load).
    later = 1_000_000.0 + 120 * DAY
    curator = SkillCurator(sm, usage, clock=lambda: later)
    plan = curator.apply_automatic_transitions()

    assert "u1/learned-thing" not in plan["archived"]
    assert pending_file.exists(), "curator must never archive/move a pending draft"


def test_curator_still_archives_stale_active_authored_skill(tmp_path, monkeypatch):
    """Regression anchor: the pending-immunity guard must not disable archiving
    of a genuinely active (promoted / non-quarantined) authored skill."""
    monkeypatch.setenv("CURATOR_STALE_DAYS", "30")
    monkeypatch.setenv("CURATOR_ARCHIVE_DAYS", "90")
    monkeypatch.setenv("SKILLS_WRITABLE_REQUIRE_REVIEW", "false")

    usage = SkillUsageStore(str(tmp_path / "skill_usage.db"), clock=lambda: 1_000_000.0)
    monkeypatch.setattr("modules.skills.skill_usage.get_skill_usage_store", lambda: usage)

    sm = SkillManager(skills_dir=tmp_path / "skills")
    result = sm.create_skill(
        "active-thing",
        "# Active Thing\n\nWhen to use it.\n\nSteps:\n1. Do the thing.\n",
        user_id="u1",
        created_by="agent",
        pending=False,
    )
    assert result.pending is False
    active_file = Path(result.path)
    assert ".pending" not in active_file.parts

    later = 1_000_000.0 + 120 * DAY
    curator = SkillCurator(sm, usage, clock=lambda: later)
    plan = curator.apply_automatic_transitions()

    assert "u1/active-thing" in plan["archived"]


# =====================================================================
# (b) SK-F8 — background review does not fire when SKILLS_WRITABLE is off
# =====================================================================

class _Host(BackgroundReviewMixin):
    def __init__(self, is_sub=False):
        self._is_sub_agent = is_sub
        self._bg_review_productive_turns = 0


def test_bg_review_does_not_fire_when_skills_writable_off(monkeypatch):
    monkeypatch.setenv("BACKGROUND_REVIEW_ENABLED", "true")
    monkeypatch.setenv("BG_REVIEW_INTERVAL", "1")
    monkeypatch.setenv("SKILLS_WRITABLE", "false")

    h = _Host()
    assert h._bg_review_should_fire(True) is False


def test_bg_review_fires_when_skills_writable_on(monkeypatch):
    monkeypatch.setenv("BACKGROUND_REVIEW_ENABLED", "true")
    monkeypatch.setenv("BG_REVIEW_INTERVAL", "1")
    monkeypatch.setenv("SKILLS_WRITABLE", "true")

    h = _Host()
    assert h._bg_review_should_fire(True) is True
