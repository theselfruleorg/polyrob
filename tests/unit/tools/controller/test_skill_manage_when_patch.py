"""`skill_manage` patch with `when` alone changes an existing skill's index line.

Prod 2026-10-31: the post-run background reviewer sent action=patch with only
`when` (and no old_string/new_string) 5-7 times per run. `when` worked only on
create, load_skill strips the frontmatter and edit keeps it, so there was no
call that could change the index line at all.
"""
import asyncio
import logging
from types import SimpleNamespace

from core.skills.frontmatter import parse_frontmatter, when_line
from modules.skills.writer import SkillWriteResult
from tools.controller import skill_manage as sm

SKILL = ('---\nname: x-engagement\ndescription: "X work"\nmetadata:\n'
         '  polyrob-when: "old line"\n  other: 1\n---\n# X\nbody stays\n')


def test_set_when_replaces_only_the_index_line():
    out = sm._set_when("x-engagement", SKILL, "any X inbox, post or DM task")
    meta, _ = parse_frontmatter(out)
    assert when_line(meta) == "any X inbox, post or DM task"
    assert out.endswith("---\n# X\nbody stays\n") and "other: 1" in out


def test_set_when_adds_a_missing_line():
    bare = "---\nname: s\ndescription: d\n---\nbody\n"
    assert when_line(parse_frontmatter(sm._set_when("s", bare, "new"))[0]) == "new"


class _FakeSM:
    def __init__(self):
        self.written = None

    def edit_skill(self, skill_id, content, *, keep_frontmatter=None, expected_revision=None, **kw):
        self.written = keep_frontmatter(SKILL, content)
        self.revision = expected_revision
        return SkillWriteResult(skill_id, True, pending=False, revision="abc")


def _run(monkeypatch, **params):
    fake = _FakeSM()
    import agents.task.agent.skill_manager as skm
    monkeypatch.setattr(skm, "get_skill_manager", lambda: fake)
    import tools.controller._helpers as h
    monkeypatch.setattr(h, "self_mod_emitter", lambda *a, **k: (lambda *a2, **k2: None))
    import tools.controller.doc_authoring as da
    monkeypatch.setattr(da, "skill_saved_text", lambda sid, where: f"saved {sid} {where}")
    controller = SimpleNamespace(logger=logging.getLogger("t"), user_id="u1",
                                 _activated_skills=set(), container=None)
    action = sm.SkillManageAction(action="patch", skill_id="x-engagement", **params)
    return fake, asyncio.run(sm.run_skill_manage(controller, action, None))


def test_patch_when_alone_rewrites_the_index_line(monkeypatch):
    fake, res = _run(monkeypatch, when="any X inbox, post or DM task", expected_sha256="s1")
    assert res.error is None, res.error
    assert when_line(parse_frontmatter(fake.written)[0]) == "any X inbox, post or DM task"
    assert fake.revision == "s1"


def test_patch_when_too_long_is_a_clear_refusal(monkeypatch):
    fake, res = _run(monkeypatch, when="x" * 61, expected_sha256="s1")
    assert fake.written is None and "<= 60" in res.error


def test_patch_without_strings_or_when_names_both_ways(monkeypatch):
    _fake, res = _run(monkeypatch, expected_sha256="s1")
    assert "old_string" in res.error and "`when` alone" in res.error
