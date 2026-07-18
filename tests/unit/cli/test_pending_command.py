"""T4-06b / T4-07 (2026-07-06 structural review): the REPL had no verb to
review self-evolution pending items — `polyrob owner pending/promote/reject`
exists only as a separate CLI command, and `/skills approve` is the
marketplace-install quarantine (a different pipeline). `/pending` is the
umbrella owner review queue inside the REPL.
"""
import types

import pytest

from agents.task.agent.skill_manager import SkillManager
from agents.task.agent.skill_writer import PROVENANCE_BACKGROUND

_BODY = "# Pending Skill\n\nA reusable procedure awaiting review, long enough to pass.\n"


class _Recorder:
    def __init__(self):
        self.lines = []

    def print_block(self, text, *, title="", style=""):
        self.lines.append(text)

    @property
    def text(self):
        return "\n".join(self.lines)


def _ctx(tmp_path, args, user_id="u1"):
    from cli.ui.commands.registry import CommandContext

    rec = _Recorder()
    ctx = CommandContext(
        renderer=rec,
        container=types.SimpleNamespace(config=types.SimpleNamespace(data_dir=str(tmp_path))),
        user_id=user_id,
        args=list(args),
    )
    return ctx, rec


@pytest.fixture()
def seeded_sm(tmp_path, monkeypatch):
    sm = SkillManager(skills_dir=tmp_path / "skills")
    res = sm.create_skill("draft-skill", _BODY, user_id="u1",
                          created_by=PROVENANCE_BACKGROUND)
    assert res.pending
    monkeypatch.setattr("agents.task.agent.skill_manager.get_skill_manager", lambda: sm)
    return sm


def test_pending_is_registered():
    from cli.ui.commands.handlers import build_default_registry

    reg = build_default_registry()
    assert reg.lookup("pending") is not None


def test_pending_lists_background_authored_skill(tmp_path, seeded_sm):
    from cli.ui.commands.handlers import _h_pending

    ctx, rec = _ctx(tmp_path, [])
    _h_pending(ctx)
    assert "draft-skill" in rec.text
    assert "approve" in rec.text.lower()  # teaches the next verb


def test_pending_approve_promotes(tmp_path, seeded_sm):
    from cli.ui.commands.handlers import _h_pending

    ctx, rec = _ctx(tmp_path, ["approve", "skill", "draft-skill"])
    _h_pending(ctx)
    skills_root = tmp_path / "skills"
    assert (skills_root / "user_u1" / "draft-skill" / "SKILL.md").exists()
    assert not (skills_root / "user_u1" / ".pending" / "draft-skill" / "SKILL.md").exists()
    assert "promoted" in rec.text.lower()


def test_pending_reject_archives(tmp_path, seeded_sm):
    from cli.ui.commands.handlers import _h_pending

    ctx, rec = _ctx(tmp_path, ["reject", "skill", "draft-skill"])
    _h_pending(ctx)
    skills_root = tmp_path / "skills"
    assert not (skills_root / "user_u1" / ".pending" / "draft-skill" / "SKILL.md").exists()
    assert not (skills_root / "user_u1" / "draft-skill" / "SKILL.md").exists()
    assert "reject" in rec.text.lower()


def test_pending_denies_non_owner(tmp_path, seeded_sm, monkeypatch):
    from cli.ui.commands.handlers import _h_pending

    monkeypatch.setattr("core.instance.is_owner", lambda *a, **k: False)
    ctx, rec = _ctx(tmp_path, ["approve", "skill", "draft-skill"])
    _h_pending(ctx)
    assert (tmp_path / "skills" / "user_u1" / ".pending" / "draft-skill" / "SKILL.md").exists()
    assert "owner" in rec.text.lower()


def test_pending_empty_queue_message(tmp_path, monkeypatch):
    from cli.ui.commands.handlers import _h_pending

    sm = SkillManager(skills_dir=tmp_path / "skills")
    monkeypatch.setattr("agents.task.agent.skill_manager.get_skill_manager", lambda: sm)
    ctx, rec = _ctx(tmp_path, [])
    _h_pending(ctx)
    assert "no pending" in rec.text.lower()


# ------------------------------------------------------------------ T3-09 show

_LONG_BODY = ("# Long Pending Skill\n\n" + ("A reusable step. " * 30)
              + "\nUNIQUE-TAIL-MARKER beyond any preview cap.\n")


@pytest.fixture()
def seeded_long_sm(tmp_path, monkeypatch):
    sm = SkillManager(skills_dir=tmp_path / "skills")
    res = sm.create_skill("long-skill", _LONG_BODY, user_id="u1",
                          created_by=PROVENANCE_BACKGROUND)
    assert res.pending
    monkeypatch.setattr("agents.task.agent.skill_manager.get_skill_manager", lambda: sm)
    return sm


def test_self_evolution_show_returns_full_body(tmp_path, seeded_long_sm):
    from core import self_evolution as se

    ok, body = se.show("skill", "long-skill", user_id="u1",
                       home_dir=tmp_path, instance_id="rob")
    assert ok
    assert "UNIQUE-TAIL-MARKER" in body  # full body, not the truncated preview


def test_self_evolution_show_unknown_item(tmp_path, seeded_sm):
    from core import self_evolution as se

    ok, msg = se.show("skill", "nope", user_id="u1",
                      home_dir=tmp_path, instance_id="rob")
    assert not ok
    assert "no pending" in msg.lower()


def test_pending_show_renders_full_body(tmp_path, seeded_long_sm):
    from cli.ui.commands.handlers import _h_pending

    ctx, rec = _ctx(tmp_path, ["show", "skill", "long-skill"])
    _h_pending(ctx)
    assert "UNIQUE-TAIL-MARKER" in rec.text


# ------------------------------------------------------------------
# owner-UX P2-4 final review, item 4: the REPL /pending list used to label
# every NON-self_context proposal "[skill]" — owner_doc/contract/pref_change
# proposals were mislabeled. Both now render with correct, kind-specific
# labels (core.self_evolution.pending_kind_label — the shared map).
# ------------------------------------------------------------------


def test_pending_labels_contract_and_pref_change_correctly(tmp_path):
    from cli.ui.commands.handlers import _h_pending
    from core.contract_writer import ContractWriter
    from core.prefs import propose_pref_change

    ContractWriter(tmp_path, instance_id="rob").propose(
        "Never spend more than $5 without asking.", user_id="u1",
        created_by="user", pending=True)
    ok, result = propose_pref_change("u1", "approvals.require", None, tmp_path,
                                     instance_id="rob", op="remove_entry",
                                     entry="git_push")
    assert ok, result

    ctx, rec = _ctx(tmp_path, [])
    _h_pending(ctx)
    assert "[contract] contract:u1" in rec.text
    assert "[pref change] pref_change:approvals.require" in rec.text
    # never mislabeled as the generic pre-fix fallback
    assert "[skill] contract:" not in rec.text
    assert "[skill] pref_change:" not in rec.text
