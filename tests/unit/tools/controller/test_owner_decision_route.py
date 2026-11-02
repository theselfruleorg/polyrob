"""095 W10 — one route per owner decision (D1 = (a)).

* A programme-term change in a lane's pinned skill also drafts the retire of
  the owner.md line on the same subject, and both drafts are ONE decision:
  one tap promotes both or neither (R8: supersede, do not overwrite).
* Every pending draft names who drafted it (R9).
* The routing bullets follow the session's registered actions (X-8, SC-10).
* ``load_skill`` honours ``polyrob-requires`` like a pin (IN-8).
"""
import hashlib
import json
import logging
import sqlite3
import types

import agents.task.agent.service  # noqa: F401 — avoid import cycle
import pytest

from agents.task.agent.skill_manager import SkillManager
from tools.controller.registry.service import Registry
from tools.controller.service import Controller

SKILL = ("---\nname: buy-lane\ndescription: The buy programme.\nmetadata:\n"
         "  polyrob-when: \"Running the buy programme\"\n---\n"
         "# Buy lane\n\nBuy 0.1 ETH of the token every day at noon.\n")
OWNER = ("# Owner rules\n\n- Reply in plain English.\n\n"
         "- The buy-lane budget is 0.1 ETH per day.\n")


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("POLYROB_DATA_DIR", str(home))
    monkeypatch.setenv("SKILLS_WRITABLE", "true")
    monkeypatch.setenv("SKILLS_WRITABLE_REQUIRE_REVIEW", "false")
    monkeypatch.setenv("SKILL_OVERWRITE_PROTECT", "false")
    monkeypatch.setenv("OWNER_DOC_WRITABLE", "true")
    sm = SkillManager(skills_dir=tmp_path / "skills")
    monkeypatch.setattr("agents.task.agent.skill_manager.get_skill_manager", lambda: sm)
    c = object.__new__(Controller)
    c.logger = logging.getLogger("owner-decision-route")
    c.registry = Registry()
    c.user_id = "u1"
    c.session_id = "s1"
    c.container = types.SimpleNamespace(config=types.SimpleNamespace(data_dir=str(home)))
    c._register_skill_manage_action()
    action = c.registry.registry.actions["skill_manage"]
    ctx = types.SimpleNamespace(user_id="u1", is_sub_agent=False, role="orchestrator",
                                metadata={})

    async def call(**kw):
        return await action.function(action.param_model(**kw), execution_context=ctx)
    return sm, home, call


def _pin(home, skill_id):
    con = sqlite3.connect(str(home / "cron.db"))
    con.execute("CREATE TABLE cron_jobs (id TEXT, task TEXT, payload TEXT, "
                "enabled INTEGER, user_id TEXT)")
    con.execute("INSERT INTO cron_jobs VALUES ('j1', 'run the buy lane', ?, 1, 'u1')",
                (json.dumps({"skills": [skill_id]}),))
    con.commit()
    con.close()


@pytest.mark.asyncio
async def test_one_card_promotes_skill_terms_and_retires_owner_line(env, monkeypatch):
    from core import self_evolution as se
    from core.instance import resolve_instance_id
    from core.owner_doc_writer import OwnerDocWriter
    sm, home, call = env
    iid = resolve_instance_id()
    assert (await call(action="create", skill_id="buy-lane", content=SKILL)).error is None
    _pin(home, "buy-lane")
    writer = OwnerDocWriter(home, instance_id=iid)
    assert writer.propose(OWNER, user_id="u1", created_by="user", pending=False).ok

    monkeypatch.setenv("SKILLS_WRITABLE_REQUIRE_REVIEW", "true")
    monkeypatch.setenv("SKILL_OVERWRITE_PROTECT", "true")
    body = sm._load_skill_content("buy-lane", user_id="u1")
    res = await call(action="patch", skill_id="buy-lane", old_string="0.1 ETH",
                     new_string="0.2 ETH", expected_sha256=_sha(SKILL))
    assert res.error is None, res.error
    assert "ONE decision" in res.extracted_content
    assert "buy-lane budget" in res.extracted_content
    assert "0.1 ETH" in body  # the active skill is untouched until the tap

    items = se.list_pending("u1", home_dir=home, instance_id=iid, skill_manager=sm)
    by_kind = {i["kind"]: i for i in items}
    assert set(by_kind) == {"skill", "owner_doc"}
    assert by_kind["skill"]["linked"] == "owner_doc:u1"
    assert by_kind["owner_doc"]["linked"] == "skill:buy-lane"
    # R9: provenance rides the preview every seat shows
    assert "drafted by the agent" in by_kind["owner_doc"]["preview"]
    note = se.build_pending_notification(items)
    assert "1 change(s)" in note and note.count("/approve_") == 2  # one entry + approve_all

    ok, msg = se.promote("skill", "buy-lane", user_id="u1", home_dir=home,
                         instance_id=iid, skill_manager=sm)
    assert ok, msg
    assert se.list_pending("u1", home_dir=home, instance_id=iid, skill_manager=sm) == []
    active = writer.read_active("u1")
    rules, superseded = active.split("## Superseded", 1)
    assert "buy-lane budget" not in rules and "buy-lane budget" in superseded
    assert "Reply in plain English" in rules
    assert "0.2 ETH" in sm._load_skill_content("buy-lane", user_id="u1")


@pytest.mark.asyncio
async def test_rejecting_either_half_rejects_both(env, monkeypatch):
    from core import self_evolution as se
    from core.instance import resolve_instance_id
    from core.owner_doc_writer import OwnerDocWriter
    sm, home, call = env
    iid = resolve_instance_id()
    await call(action="create", skill_id="buy-lane", content=SKILL)
    _pin(home, "buy-lane")
    writer = OwnerDocWriter(home, instance_id=iid)
    writer.propose(OWNER, user_id="u1", created_by="user", pending=False)
    monkeypatch.setenv("SKILLS_WRITABLE_REQUIRE_REVIEW", "true")
    monkeypatch.setenv("SKILL_OVERWRITE_PROTECT", "true")
    await call(action="patch", skill_id="buy-lane", old_string="0.1 ETH",
               new_string="0.2 ETH", expected_sha256=_sha(SKILL))
    ok, msg = se.reject("owner_doc", "u1", user_id="u1", home_dir=home,
                        instance_id=iid, skill_manager=sm)
    assert ok, msg
    assert se.list_pending("u1", home_dir=home, instance_id=iid, skill_manager=sm) == []
    assert "buy-lane budget" in writer.read_active("u1")


@pytest.mark.asyncio
async def test_an_unpinned_skill_drafts_no_owner_retire(env, monkeypatch):
    from core.instance import resolve_instance_id
    from core.owner_doc_writer import OwnerDocWriter
    sm, home, call = env
    await call(action="create", skill_id="buy-lane", content=SKILL)
    writer = OwnerDocWriter(home, instance_id=resolve_instance_id())
    writer.propose(OWNER, user_id="u1", created_by="user", pending=False)
    monkeypatch.setenv("SKILLS_WRITABLE_REQUIRE_REVIEW", "true")
    monkeypatch.setenv("SKILL_OVERWRITE_PROTECT", "true")
    res = await call(action="patch", skill_id="buy-lane", old_string="0.1 ETH",
                     new_string="0.2 ETH", expected_sha256=_sha(SKILL))
    assert res.error is None and "Also drafted" not in res.extracted_content
    assert writer.list_pending("u1") is None


def test_routing_bullets_follow_the_registered_actions(monkeypatch):
    from agents.task.agent.prompts import SystemPrompt
    for flag in ("PREFS_TOOL_ENABLED", "OWNER_DOC_WRITABLE", "SKILLS_WRITABLE",
                 "SELF_CONTEXT_WRITABLE"):
        monkeypatch.setenv(flag, "true")
    only_owner_doc = "- owner_doc_manage: Record the owner's rules\n- preferences: read\n"
    sp = SystemPrompt(action_description=only_owner_doc, tool_ids=[])
    assert sp._owner_route_parts() == ["prefs", "owner_doc"]
    # SC-10: prefs on with SKILLS_WRITABLE off never names skill_manage
    monkeypatch.setenv("SKILLS_WRITABLE", "false")
    assert "programme" not in SystemPrompt(action_description="", tool_ids=[])._owner_route_parts()
    # a session with none of the writers gets no routing block at all
    sp = SystemPrompt(action_description="- send_message: talk\n", tool_ids=[])
    assert sp._owner_route_parts() == []


def test_tool_descriptions_quote_the_one_routing_text(env):
    from core.prompt_text import owner_route
    from tools.controller.skill_manage import SKILL_MANAGE_DESCRIPTION
    assert owner_route("programme") in SKILL_MANAGE_DESCRIPTION
    assert "`skill_manage` also drafts the retire" in owner_route("programme")


def test_load_skill_names_its_prerequisites(tmp_path):
    from agents.task.agent.skill_manager import MatchedSkill
    from tools.controller._helpers import append_prerequisites, build_load_skill_result

    class _SM:
        def prerequisites_of(self, sid, *, user_id, tool_ids):
            assert sid == "lane"
            return [MatchedSkill(skill_id="safety", priority=1, match_reasons=[],
                                 content="# Safety\n\nCheck first.")]

        def resolve_skill_dir(self, sid, uid):
            return None

    ctl = types.SimpleNamespace(_activated_skills=set(), _session_skills={},
                                list_tools=lambda: [])
    lane = MatchedSkill(skill_id="lane", priority=1, match_reasons=[], content="# Lane")
    res = build_load_skill_result({"lane": lane}, "lane", activated=ctl._activated_skills)
    res = append_prerequisites(res, ctl, _SM(), "u1")
    assert '<skill id="safety">' in res.extracted_content
    assert res.metadata["skill_prerequisites"] == ["safety"]
    # already held this session: not sent twice
    res2 = build_load_skill_result({"lane": lane}, "lane", activated=set())
    res2 = append_prerequisites(res2, ctl, _SM(), "u1")
    assert "skill_prerequisites" not in res2.metadata
