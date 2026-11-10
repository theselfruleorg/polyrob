"""095 W13, 108 W6 — the background reviewer runs AFTER a goal / cron run, on a bad
signal only, patch-first, never on an owner or self document."""
import asyncio
import types

import pytest

import agents.task.agent.core.background_review as br
from agents.task.session_class import (
    AUTONOMOUS, CHILD, CORRESPONDENT, CRON, GOAL, OWNER_CHAT, REGIME_POLICY, ROOM)


@pytest.fixture
def review_on(monkeypatch):
    monkeypatch.setenv("BACKGROUND_REVIEW_ENABLED", "true")
    monkeypatch.setenv("SKILLS_WRITABLE", "true")
    monkeypatch.setattr(br, "_drafts_since", lambda user_id, since_ts: [])
    monkeypatch.setattr(br, "lane_skill_blocks", lambda *a, **k: "")

    async def _no_measure(*a, **k):
        return 0
    import agents.task.agent.core.lane_measure as LM
    import modules.skills.skill_usage as SU
    monkeypatch.setattr(LM, "record_changes", _no_measure)
    monkeypatch.setattr(SU, "get_skill_usage_store", lambda *a, **k: None)


class _Mgr:
    def __init__(self, gate=None):
        self.calls = []
        self.gate = gate

    async def run_subtask(self, **kw):
        self.calls.append(kw)
        if self.gate is not None:
            await self.gate.wait()


def _task_agent(mgr, *, sub=False):
    agent = types.SimpleNamespace(
        _is_sub_agent=sub, agent_id="main", llm="MAIN", _judge_llm="AUX",
        history=types.SimpleNamespace(history=[]))
    orch = types.SimpleNamespace(agents={"main": agent},
                                 get_sub_agent_manager=lambda: mgr)
    return types.SimpleNamespace(get_orchestrator=lambda sid: orch)


_VERIFIED_GOAL = {"signal_source": "judge", "verified": "unverified", "useful": False,
                  "skills_loaded": ["report-writing"], "skill_gens": {"report-writing": 3},
                  "lane": "report-writing"}


def _review(task_agent, regime=GOAL, meta=None, outcome="done", pinned=None):
    return asyncio.run(br.review_after_run(
        task_agent, regime=regime, session_id="s1", user_id="u1", rail_id="goal:g1",
        outcome=outcome, meta=_VERIFIED_GOAL if meta is None else meta, pinned=pinned))


def test_reviewer_runs_after_a_failed_verified_goal_run(review_on):
    mgr = _Mgr()
    assert _review(_task_agent(mgr), pinned=["lane-skill"]) is True
    (call,) = mgr.calls
    assert call["parent_llm"] == "AUX"  # the aux (judge) model, not the main one
    assert "verified=unverified" in call["task"] and "signal=judge" in call["task"]
    assert "report-writing@g3" in call["task"]  # loaded skills with patch generation
    assert "FIRST PINNED SKILL OF THIS RUN: lane-skill" in call["task"]


def test_reviewer_runs_outside_the_run_budget(review_on):
    from core.config_policy import AutonomyConfig
    mgr = _Mgr()
    _review(_task_agent(mgr))
    # Its own small budget, never the run's (the run has already ended).
    assert mgr.calls[0]["max_steps"] == AutonomyConfig.bg_review_max_steps()

    async def _flow():
        gate = asyncio.Event()
        slow = _Mgr(gate)
        br.schedule_review_after_run(_task_agent(slow), regime=GOAL, session_id="s1",
                                     user_id="u1", rail_id="goal:g1", outcome="done",
                                     meta=_VERIFIED_GOAL)
        # The caller (the run's record funnel) returns before the review runs.
        assert slow.calls == []
        await asyncio.sleep(0.01)
        assert len(slow.calls) == 1  # started on its own task
        gate.set()
        await asyncio.sleep(0.01)

    asyncio.run(_flow())


@pytest.mark.parametrize("meta", [
    {"signal_source": "judge", "verified": "verified", "useful": False},
    {"signal_source": "judge", "verified": True, "useful": False},
    {"signal_source": "receipt", "useful": True},
    {"signal_source": "owner", "owner_verdict": "confirm", "useful": False},
    {"signal_source": "self", "useful": False},
    {"signal_source": "none"},
])
def test_a_plain_success_gets_no_review(review_on, meta):
    mgr = _Mgr()
    assert br.review_after_run_due(GOAL, meta, "done") is False
    assert br.review_after_run_due(CRON, meta, "done") is False
    assert _review(_task_agent(mgr), meta=meta) is False
    assert mgr.calls == []


@pytest.mark.parametrize("meta, outcome, signal", [
    ({"owner_verdict": "correct", "useful": True}, "done", "owner_correct"),
    ({"verified": False, "useful": True}, "done", "verified_failed"),
    ({"verified": "unverified"}, "done", "verified_failed"),
    ({"stop_reason": "max_steps", "signal_source": "self"}, "partial", "run_error"),
    ({"error": "a tool raised"}, "failed", "run_error"),
    ({}, "failed", "run_error"),
])
def test_each_bad_signal_earns_a_review(meta, outcome, signal):
    assert br.review_signal(meta, outcome) == signal
    assert br.review_after_run_due(CRON, meta, outcome) is True


@pytest.mark.parametrize("stop", ["cancelled", "control", "awaiting_reply"])
def test_an_owner_stop_or_a_pause_is_not_a_run_error(stop):
    assert br.review_signal({"stop_reason": stop, "error": "cut"}, "failed") == ""


def test_reviewer_prefers_patch_of_loaded_skill():
    p = br.build_run_review_prompt("", outcome_line=br.run_outcome_line("done", _VERIFIED_GOAL))
    patch, pinned, create = p.index("(a) PATCH"), p.index("(b)"), p.index("(c)")
    assert patch < pinned < create
    assert "action='patch'" in p and "expected_sha256" in p
    assert "smallest span" in p
    assert "live for the next run" in p


def test_reviewer_never_writes_owner_or_self_docs(monkeypatch):
    monkeypatch.setenv("SELF_CONTEXT_WRITABLE", "true")
    for p in (br.build_review_prompt("step 1: x"),
              br.build_run_review_prompt("step 1: x", outcome_line="outcome=done")):
        assert "self_context_manage" not in p
        assert "owner_doc_manage" not in p
    assert not hasattr(br, "_SELF_CONTEXT_REVIEW_ADDENDUM")


def test_review_after_run_column():
    assert REGIME_POLICY[GOAL].review_after_run is True
    assert REGIME_POLICY[CRON].review_after_run is True
    for regime in (OWNER_CHAT, AUTONOMOUS, ROOM, CORRESPONDENT, CHILD):
        assert REGIME_POLICY[regime].review_after_run is False


@pytest.mark.parametrize("regime", [CHILD, ROOM, CORRESPONDENT, OWNER_CHAT, AUTONOMOUS])
def test_not_on_leaf_room_or_correspondent(review_on, regime):
    mgr = _Mgr()
    assert br.review_after_run_due(regime, _VERIFIED_GOAL) is False
    assert _review(_task_agent(mgr), regime=regime) is False
    assert mgr.calls == []


@pytest.mark.parametrize("orch_attr", ["_public_session", "_correspondent_session"])
def test_inline_reviewer_skips_room_and_correspondent(monkeypatch, orch_attr):
    monkeypatch.setenv("BACKGROUND_REVIEW_ENABLED", "true")
    monkeypatch.setenv("SKILLS_WRITABLE", "true")
    monkeypatch.setenv("BG_REVIEW_INTERVAL", "1")
    host = types.SimpleNamespace(session_id=None, _is_sub_agent=False,
                                 _bg_review_productive_turns=0,
                                 orchestrator=types.SimpleNamespace(**{orch_attr: True}))
    assert br.BackgroundReviewMixin._bg_review_should_fire(host, True) is False


def test_gates_still_apply(monkeypatch):
    monkeypatch.setenv("BACKGROUND_REVIEW_ENABLED", "false")
    monkeypatch.setenv("SKILLS_WRITABLE", "true")
    mgr = _Mgr()
    assert _review(_task_agent(mgr)) is False and mgr.calls == []


def test_a_leaf_session_is_never_the_reviewed_agent(review_on):
    mgr = _Mgr()
    assert _review(_task_agent(mgr, sub=True)) is False  # no main agent: not a subagent
    assert mgr.calls == []


def test_a_review_adds_no_second_row_to_a_change(review_on, monkeypatch):
    """One change, one row: the patch row is the owner's line; the reviewer adds nothing."""
    monkeypatch.setattr(br, "_drafts_since",
                        lambda user_id, since_ts: ["patch report-writing"])
    seen = []
    import core.self_events as se
    monkeypatch.setattr(se, "emit_self_modification", lambda **kw: seen.append(kw))
    _review(_task_agent(_Mgr()))
    assert seen == []


def test_no_owner_line_when_nothing_was_drafted(review_on, monkeypatch):
    seen = []
    import core.self_events as se
    monkeypatch.setattr(se, "emit_self_modification", lambda **kw: seen.append(kw))
    _review(_task_agent(_Mgr()))
    assert seen == []


# --- review fixes: the writers are gone from the child; one reviewer per user ---

def test_both_reviewer_paths_drop_the_owner_and_self_doc_writers(review_on):
    mgr = _Mgr()
    _review(_task_agent(mgr))
    assert {"owner_doc_manage", "self_context_manage"} <= set(mgr.calls[0]["exclude_actions"])

    inline = types.SimpleNamespace(
        orchestrator=types.SimpleNamespace(get_sub_agent_manager=lambda: mgr),
        _judge_llm="AUX", llm="MAIN", agent_id="main",
        history=types.SimpleNamespace(history=[]))
    asyncio.run(br.BackgroundReviewMixin._run_background_review(inline))
    assert {"owner_doc_manage", "self_context_manage"} <= set(mgr.calls[1]["exclude_actions"])


def test_reviewer_child_registry_has_no_owner_or_self_doc_writer(monkeypatch):
    """078 D7 in code: the child controller built for the reviewer never holds the
    writers, even when SUBAGENT_LEAST_PRIVILEGE is off (an exclusion forces a child)."""
    import logging
    from agents.task.agent.sub_agent_manager import SubAgentManager
    from agents.task.constants import TimeoutConfig
    from tools.controller.service import Controller
    from core.config_policy import AutonomyConfig
    monkeypatch.setattr(TimeoutConfig, "get_subagent_least_privilege",
                        classmethod(lambda cls: False))
    monkeypatch.setattr(AutonomyConfig, "owner_doc_writable", classmethod(lambda cls: True))
    monkeypatch.setattr(AutonomyConfig, "self_context_writable",
                        classmethod(lambda cls: True))
    m = object.__new__(SubAgentManager)
    m.logger = logging.getLogger("reviewer-child-test")
    orch = types.SimpleNamespace(session_id="s1", user_id="u1", workspace_dir="/tmp",
                                 container=None)
    parent = Controller(orchestrator=orch)
    held = parent.registry.registry.actions
    assert "owner_doc_manage" in held and "self_context_manage" in held  # writers exist
    orch.controller = parent
    m.orchestrator = orch

    async def _no_load(self, tool_ids):
        return None
    monkeypatch.setattr(Controller, "load_tools_from_container", _no_load)
    child = asyncio.run(m._build_child_controller(
        force=True, exclude_actions=br.REVIEWER_EXCLUDED_ACTIONS))
    actions = child.registry.registry.actions
    assert "owner_doc_manage" not in actions and "self_context_manage" not in actions
    assert "skill_manage" in actions or "load_skill" in actions


def test_a_burst_of_runs_spawns_one_reviewer_per_user(review_on):
    async def _flow():
        gate = asyncio.Event()
        slow = _Mgr(gate)
        for i in range(4):
            br.schedule_review_after_run(_task_agent(slow), regime=GOAL, session_id="s1",
                                         user_id="u1", rail_id=f"goal:g{i}",
                                         outcome="done", meta=_VERIFIED_GOAL)
        br.schedule_review_after_run(_task_agent(slow), regime=GOAL, session_id="s2",
                                     user_id="u2", rail_id="goal:h", outcome="done",
                                     meta=_VERIFIED_GOAL)
        await asyncio.sleep(0.01)
        assert len(slow.calls) == 2  # one for u1 (three skipped), one for u2
        gate.set()
        await asyncio.sleep(0.01)
        assert br._REVIEWS_IN_FLIGHT == set()  # released when the review ends
        br.schedule_review_after_run(_task_agent(slow), regime=GOAL, session_id="s1",
                                     user_id="u1", rail_id="goal:g9", outcome="done",
                                     meta=_VERIFIED_GOAL)
        await asyncio.sleep(0.01)
        assert len(slow.calls) == 3

    asyncio.run(_flow())
