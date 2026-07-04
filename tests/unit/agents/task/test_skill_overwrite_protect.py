"""Tests for non-destructive skill writes (archive-on-overwrite + active-overwrite → .pending).

Task 2 — SKILL_OVERWRITE_PROTECT guard:
- An agent/background overwrite of an existing ACTIVE skill must be forced to .pending
  (not silently clobber the curated body).
- Any overwrite (including owner) archives the prior body to .archived/<skill_id>/.
- BUG 2 fix: overwriting a SYSTEM skill id (in skill_rules) also forces .pending for
  non-user authors, even if no user-side active file exists yet.
- BUG 5 fix: two rapid overwrites of the same skill produce two distinct archive files.
"""
from agents.task.agent.skill_manager import SkillManager
from agents.task.agent.skill_writer import PROVENANCE_AGENT, PROVENANCE_USER

GOOD = "# My Skill\nDo X then Y. " + ("body " * 20)
STALE = "# My Skill\nDo X then WRONG. " + ("body " * 20)


def _mk(tmp_path):
    sm = SkillManager()
    sm.skills_dir = tmp_path
    return sm


def test_agent_cannot_silently_overwrite_active_skill(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILL_OVERWRITE_PROTECT", "true")
    sm = _mk(tmp_path)
    # Owner creates an ACTIVE skill.
    r1 = sm.create_skill("my-skill", GOOD, user_id="u1", created_by=PROVENANCE_USER, pending=False)
    assert r1.ok and not r1.pending
    active = tmp_path / "user_u1" / "my-skill" / "SKILL.md"
    assert active.read_text() == GOOD
    # Agent tries to overwrite with stale content → forced to .pending, active untouched.
    r2 = sm.create_skill("my-skill", STALE, user_id="u1", created_by=PROVENANCE_AGENT, pending=False)
    assert r2.ok and r2.pending
    assert active.read_text() == GOOD  # NOT clobbered
    assert (tmp_path / "user_u1" / ".pending" / "my-skill" / "SKILL.md").read_text() == STALE


def test_owner_overwrite_archives_prior_body(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILL_OVERWRITE_PROTECT", "true")
    sm = _mk(tmp_path)
    sm.create_skill("my-skill", GOOD, user_id="u1", created_by=PROVENANCE_USER, pending=False)
    sm.create_skill("my-skill", STALE, user_id="u1", created_by=PROVENANCE_USER, pending=False)
    archived = tmp_path / "user_u1" / ".archived" / "my-skill"
    bodies = [p.read_text() for p in archived.glob("*-SKILL.md")]
    assert GOOD in bodies  # prior body preserved
    assert (tmp_path / "user_u1" / "my-skill" / "SKILL.md").read_text() == STALE  # owner overwrite applied


# ── BUG 2 fix: system-skill id shadows must be forced to .pending ────────────

def test_agent_cannot_silently_shadow_system_skill(tmp_path, monkeypatch):
    """BUG 2: overwriting a curated SYSTEM skill id (present in skill_rules but with NO
    user-side active file) must be forced to .pending when SKILL_OVERWRITE_PROTECT=true.
    Before the fix, overwriting_active was always False here (active_file.exists() == False)
    so a PROVENANCE_AGENT write would land ACTIVE, silently shadowing the system skill.
    """
    monkeypatch.setenv("SKILL_OVERWRITE_PROTECT", "true")
    sm = _mk(tmp_path)
    # Seed skill_rules to simulate a curated system skill — no actual file on disk.
    sm.skill_rules = {"web-research": {"name": "Web Research", "description": "curated"}}
    body = "# Web Research\nAgent-authored override. " + ("x " * 20)
    result = sm.create_skill(
        "web-research", body, user_id="u1",
        created_by=PROVENANCE_AGENT, pending=False,
    )
    assert result.ok, f"expected ok but got errors: {result.errors}"
    assert result.pending, "overwrite of a system skill id must be forced to .pending"
    # No active file written.
    assert not (tmp_path / "user_u1" / "web-research" / "SKILL.md").exists()
    # Pending file IS there.
    assert (tmp_path / "user_u1" / ".pending" / "web-research" / "SKILL.md").exists()


# ── BUG 5 fix: collision-safe archive names ───────────────────────────────────

def test_two_rapid_overwrites_produce_distinct_archives(tmp_path, monkeypatch):
    """BUG 5: if n-SKILL.md already exists (e.g. from a concurrent session), the second
    archive must NOT silently clobber it — it must fall back to a unique name.
    """
    monkeypatch.setenv("SKILL_OVERWRITE_PROTECT", "false")
    sm = _mk(tmp_path)
    body_v1 = "# Skill\nVersion 1. " + ("x " * 20)
    body_v2 = "# Skill\nVersion 2. " + ("y " * 20)
    body_v3 = "# Skill\nVersion 3. " + ("z " * 20)
    sm.create_skill("archive-test", body_v1, user_id="u2", created_by=PROVENANCE_USER, pending=False)
    sm.create_skill("archive-test", body_v2, user_id="u2", created_by=PROVENANCE_USER, pending=False)
    sm.create_skill("archive-test", body_v3, user_id="u2", created_by=PROVENANCE_USER, pending=False)
    archived = tmp_path / "user_u2" / ".archived" / "archive-test"
    all_bodies = [p.read_text() for p in archived.glob("*-SKILL.md")]
    # v1 and v2 should both be present (v3 is the live version, v2 archived v1, v3 archived v2).
    assert body_v1 in all_bodies, "first archived body not found"
    assert body_v2 in all_bodies, "second archived body not found"
    # All archive files must be distinct names.
    names = [p.name for p in archived.glob("*-SKILL.md")]
    assert len(names) == len(set(names)), f"duplicate archive names: {names}"
