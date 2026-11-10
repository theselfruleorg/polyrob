"""108 W6: a refused patch names what it lacks and shows a correct call; a reviewer's change
names its run and signal and writes one row."""
import asyncio
import types

import pytest

from agents.task.agent.skill_manager import SkillManager
from modules.skills.skill_usage import reset_skill_usage_store
from modules.skills.writer import REVIEW_SCOPE
from tools.controller.skill_manage import SkillManageAction, run_skill_manage

BODY = "---\nname: lane-skill\ndescription: a lane skill\n---\n# Lane skill\n\nStep one.\n"


@pytest.fixture
def sm(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    reset_skill_usage_store()
    yield SkillManager(str(tmp_path / "skills"))
    reset_skill_usage_store()


def _manage(sm, monkeypatch, **params):
    import agents.task.agent.skill_manager as M
    from tools.controller import action_registration as ar
    monkeypatch.setattr(M, "get_skill_manager", lambda: sm)
    monkeypatch.setattr(ar, "_is_forged_or_autonomous_turn", lambda ctx, c: True)
    import tools.controller.skill_manage as SMG
    monkeypatch.setattr(SMG, "_untrusted_author", lambda ctx, c: False)
    events = []
    import core.self_events as SE
    monkeypatch.setattr(SE, "emit_self_modification", lambda **kw: events.append(kw))
    controller = types.SimpleNamespace(user_id="u1", logger=types.SimpleNamespace(debug=print))
    return asyncio.run(run_skill_manage(controller, SkillManageAction(**params), None)), events


def test_refused_patch_names_what_it_lacks_and_shows_a_correct_call(sm, monkeypatch):
    res, events = _manage(sm, monkeypatch, action="patch", skill_id="lane-skill",
                          expected_sha256="abc123", reason="a reason")
    assert "this call has no `old_string` and no `new_string`" in res.error
    assert "skill_manage(action='patch', skill_id='lane-skill', expected_sha256='abc123'" in res.error
    assert "old_string='" in res.error and "reason='" in res.error
    assert "do not repeat this call" in res.error and events == []


def test_refused_patch_with_content_says_patch_does_not_read_it(sm, monkeypatch):
    res, _ = _manage(sm, monkeypatch, action="patch", skill_id="lane-skill",
                     expected_sha256="abc123", content="# whole body", reason="a reason")
    assert "`content` is for create and edit" in res.error


def test_old_string_not_found_shows_a_correct_call(sm, monkeypatch):
    assert sm.create_skill("lane-skill", BODY, user_id="u1", created_by="agent").ok
    res, _ = _manage(sm, monkeypatch, action="patch", skill_id="lane-skill",
                     expected_sha256=sm._content_revision(BODY), old_string="absent",
                     new_string="new", reason="a reason")
    assert "copied exactly" in res.error and "skill_manage(action='patch'" in res.error


# --- 3. a change names the run and the signal; 4. one change, one row --------------------

def test_a_reviewer_change_names_run_and_signal_and_writes_one_row(sm, monkeypatch):
    assert sm.create_skill("lane-skill", BODY, user_id="u1", created_by="agent").ok
    token = REVIEW_SCOPE.set("run=cron:j1 signal=owner_correct")
    try:
        res, events = _manage(sm, monkeypatch, action="patch", skill_id="lane-skill",
                              expected_sha256=sm._content_revision(BODY), old_string="Step one.",
                              new_string="Step one.\nStep two.", reason="the run skipped a step")
    finally:
        REVIEW_SCOPE.reset(token)
    assert not res.error
    (ev,) = events
    assert ev["action"] == "patch"
    assert ev["note"] == "run=cron:j1 signal=owner_correct: the run skipped a step"


def test_a_reviewer_change_with_no_reason_is_refused(sm, monkeypatch):
    assert sm.create_skill("lane-skill", BODY, user_id="u1", created_by="agent").ok
    token = REVIEW_SCOPE.set("run=cron:j1 signal=run_error")
    try:
        res, events = _manage(sm, monkeypatch, action="patch", skill_id="lane-skill",
                              expected_sha256=sm._content_revision(BODY), old_string="Step one.",
                              new_string="x")
    finally:
        REVIEW_SCOPE.reset(token)
    assert "needs `reason`" in res.error and events == []
