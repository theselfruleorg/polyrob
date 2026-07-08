"""T4-06 (2026-07-06 structural review): self-modifications (skill writes,
self-context edits, owner promotions, curator archives) surfaced only as generic
tool_execution — no first-class event, no owner surface could answer "what did
the agent change about itself?".

A `self_modification` event (kind, action, item_id, pending, created_by, ok) now
rides the durable event log from every mutation path.
"""
import asyncio
import logging
import types

import pytest

import agents.task.agent.service  # noqa: F401 — import-cycle guard
from agents.task.telemetry.event_log import TelemetryEventLog
from agents.task.telemetry.self_events import emit_self_modification


@pytest.fixture()
def log(tmp_path, monkeypatch):
    lg = TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    monkeypatch.setattr(
        "agents.task.telemetry.event_log.get_event_log", lambda db_path=None: lg
    )
    monkeypatch.delenv("TELEMETRY_EVENT_LOG_ENABLED", raising=False)
    return lg


def test_emit_self_modification_records_row(log):
    emit_self_modification(kind="skill", action="create", item_id="my-skill",
                           user_id="u1", session_id="s1", pending=True,
                           created_by="background_review", source="skill_manage")
    rows = log.query(kind="self_modification")
    assert len(rows) == 1
    a = rows[0]["attrs"]
    assert a["kind"] == "skill" and a["action"] == "create"
    assert a["item_id"] == "my-skill" and a["pending"] is True
    assert a["created_by"] == "background_review"
    assert rows[0]["source"] == "skill_manage"


def test_skill_manage_create_emits_event(log, monkeypatch, tmp_path):
    from agents.task.agent.skill_manager import SkillManager
    from tools.controller.registry.service import Registry
    from tools.controller.service import Controller

    body = "# Pending Skill\n\nA reusable procedure awaiting review, long enough to pass.\n"
    monkeypatch.setenv("SKILLS_WRITABLE", "true")
    sm = SkillManager(skills_dir=tmp_path)
    monkeypatch.setattr("agents.task.agent.skill_manager.get_skill_manager", lambda: sm)

    c = object.__new__(Controller)
    c.logger = logging.getLogger("self-mod-events-test")
    c.registry = Registry()
    c.user_id = "u1"
    c.session_id = "s1"
    c._register_skill_manage_action()

    action = c.registry.registry.actions["skill_manage"]
    ctx = types.SimpleNamespace(user_id="u1", session_id="s1",
                                is_sub_agent=True, role="leaf", metadata={})
    params = action.param_model(action="create", skill_id="draft-skill", content=body)
    res = asyncio.new_event_loop().run_until_complete(
        action.function(params, execution_context=ctx))
    assert res.error is None
    rows = log.query(kind="self_modification")
    assert len(rows) == 1
    a = rows[0]["attrs"]
    assert a["kind"] == "skill" and a["action"] == "create"
    assert a["item_id"] == "draft-skill"
    assert a["pending"] is True  # forged/leaf turn => quarantined
    assert a["created_by"] == "background_review"


def test_self_evolution_promote_emits_owner_review_event(log, monkeypatch, tmp_path):
    from core import self_evolution as se

    class _Res:
        ok = True
        pending = False
        errors = []

    mgr = types.SimpleNamespace(
        promote_pending_skill=lambda skill_id, user_id: _Res(),
    )
    ok, _msg = se.promote(se.KIND_SKILL, "draft-skill", user_id="u1",
                          home_dir=tmp_path, instance_id="rob", skill_manager=mgr)
    assert ok
    rows = log.query(kind="self_modification")
    assert len(rows) == 1
    assert rows[0]["source"] == "owner_review"
    assert rows[0]["attrs"]["action"] == "promote"
    assert rows[0]["attrs"]["kind"] == "skill"


def test_curator_archive_emits_event(log, monkeypatch):
    from agents.task.agent.core.curator import SkillCurator

    archived = []
    sm = types.SimpleNamespace(
        delete_skill=lambda skill_id, user_id, absorbed_into=None: archived.append(skill_id) or True,
        _find_skill_file=lambda user_id, skill_id: None,
    )
    usage = types.SimpleNamespace(
        list_authored=lambda created_by: [
            {"skill_id": "old-skill", "user_id": "u1", "load_count": 0,
             "created_at": 1.0},  # 0.0 is falsy -> curator substitutes now()
        ],
    )
    cur = SkillCurator(sm, usage, dry_run=False)
    # marks store: keep default; _now far in the future => age > archive_days
    monkeypatch.setattr(cur, "_now", lambda: 10_000_000.0)
    monkeypatch.setattr(cur, "_get_mark", lambda key: None)
    monkeypatch.setattr(cur, "_set_mark", lambda key, value: None)
    plan = cur.apply_automatic_transitions()
    assert plan["archived"] == ["u1/old-skill"]
    rows = log.query(kind="self_modification")
    assert len(rows) == 1
    assert rows[0]["source"] == "curator"
    assert rows[0]["attrs"]["action"] == "archive"
    assert rows[0]["attrs"]["item_id"] == "old-skill"
