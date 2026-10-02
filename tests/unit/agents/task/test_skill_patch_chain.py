"""A chain of edits to ONE skill must cost the owner ONE approval (2026-09-21).

Prod, 2026-09-21 15:35–15:36: the agent patched `rh-reporting` twice in one turn
to apply a single owner rule. Patch 1 landed pending; patch 2 was refused
``revision conflict; reload before editing``; the owner approved; the retry then
succeeded and produced a SECOND pending item; the owner approved the same skill
again. From his seat that reads as "the approval re-approves the same document".

Cause: ``patch_skill`` read the ACTIVE ``SKILL.md`` (``_find_skill_file`` prefers
it) and derived its CAS token from that body, but a non-owner patch of an active
skill is quarantined and WRITES ``.pending/<id>/SKILL.md``. The token named one
file and was checked against another, so once a draft existed no later patch
could ever pass — and the advertised remedy (``load_skill``) short-circuited with
"already active this session". Had the CAS passed, patch 2's body (computed from
the ACTIVE text) would have overwritten and discarded patch 1.
"""
import pytest

from agents.task.agent.skill_manager import SkillManager

BODY = ("# Demo\n\n## Rule 4 - old rule\n\nbody A\n\n"
        "## Deferred - needs owner sign-off\n\nbody B\n")


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLS_WRITABLE_REQUIRE_REVIEW", "true")
    return SkillManager(skills_dir=tmp_path)


def _draft(manager, uid, sid):
    return manager._read_skill_text(
        manager.skills_dir / f"user_{uid}" / ".pending" / sid / "SKILL.md")


def test_second_patch_of_an_active_skill_is_not_a_revision_conflict(manager):
    manager.create_skill("demo", BODY, user_id="u1", created_by="user", pending=False)
    first = manager.patch_skill("demo", user_id="u1", created_by="agent",
                                old_string="## Rule 4 - old rule",
                                new_string="## Rule 4 - NEW rule")
    assert first.ok and first.pending
    second = manager.patch_skill("demo", user_id="u1", created_by="agent",
                                 old_string="## Deferred - needs owner sign-off",
                                 new_string="## Owner signed off")
    assert second.ok, second.errors
    assert second.pending


def test_a_chain_of_patches_accumulates_into_one_draft(manager):
    manager.create_skill("demo", BODY, user_id="u1", created_by="user", pending=False)
    manager.patch_skill("demo", user_id="u1", created_by="agent",
                        old_string="## Rule 4 - old rule",
                        new_string="## Rule 4 - NEW rule")
    manager.patch_skill("demo", user_id="u1", created_by="agent",
                        old_string="## Deferred - needs owner sign-off",
                        new_string="## Owner signed off")
    draft = _draft(manager, "u1", "demo")
    # Both edits survive: the second was computed from the DRAFT, not the active
    # body, so it cannot discard the first.
    assert "## Rule 4 - NEW rule" in draft
    assert "## Owner signed off" in draft
    assert "## Rule 4 - old rule" not in draft
    # ONE pending item, so ONE approval.
    assert [it["skill_id"] for it in manager.list_pending_skills("u1")] == ["demo"]


def test_the_active_body_is_never_written_by_an_agent_patch(manager):
    manager.create_skill("demo", BODY, user_id="u1", created_by="user", pending=False)
    manager.patch_skill("demo", user_id="u1", created_by="agent",
                        old_string="## Rule 4 - old rule",
                        new_string="## Rule 4 - NEW rule")
    active = manager._read_skill_text(
        manager.skills_dir / "user_u1" / "demo" / "SKILL.md")
    assert active == BODY


def test_an_explicit_stale_cas_token_still_refuses(manager):
    """The quarantine CAS still protects a draft from a stale writer."""
    first = manager.create_skill("calib", "# A\n\ngauge\n", user_id="u1")
    assert first.ok and first.pending
    ok = manager.patch_skill("calib", user_id="u1", old_string="gauge",
                             new_string="sensor", expected_revision=first.revision)
    assert ok.ok
    stale = manager.patch_skill("calib", user_id="u1", old_string="sensor",
                                new_string="meter", expected_revision=first.revision)
    assert not stale.ok and "revision conflict" in stale.errors[0]
