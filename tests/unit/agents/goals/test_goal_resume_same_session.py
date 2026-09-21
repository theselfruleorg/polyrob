"""057 WS-C (B9) — a pre-empted goal restarts with its note, and can resume.

Two halves:

- UNCONDITIONAL: `payload.resume_note` was written by the yield path and read
  NOWHERE — `build_goal_run_task` had no parameter for it — so a pre-empted goal
  restarted with no idea it had been pre-empted. It now leads the task body.
- FLAG-GATED (`GOAL_RESUME_SAME_SESSION`, default OFF): the goal re-enters its
  OWN session through the existing self-wake rail
  (`_resolve_or_recreate` -> `_recreate_orchestrator` -> `load_from_disk`)
  instead of minting a cold one, with the note injected as a SYSTEM_NOTE.
"""
from types import SimpleNamespace

import pytest

from agents.task.goals.board import Goal
from agents.task.goals.context import build_goal_run_task
from agents.task.runtime.run_as_session import run_task_to_outcome


def test_the_restart_note_leads_the_task_body():
    goal = Goal(id="g1", user_id="rob", title="Round 15", body="finish the report",
                payload={"resume_note": "Yielded at 06:00Z because the rail EXIT "
                                        "came due. You had reached step 7."})
    task = build_goal_run_task(goal, None)
    assert task.startswith("RESTART NOTE (you were pre-empted):")
    assert "step 7" in task


def test_a_goal_with_no_note_is_byte_identical():
    goal = Goal(id="g1", user_id="rob", title="Round 15", body="finish the report")
    assert "RESTART NOTE" not in build_goal_run_task(goal, None)


class _ResumingAgent:
    """A task agent with the self-wake resume rail."""

    def __init__(self, *, known=True, owner="rob"):
        self.pushed = []
        self.ran = []
        self.created = []
        agent = SimpleNamespace(
            message_manager=SimpleNamespace(
                push_ephemeral_message=self.pushed.append),
            _is_sub_agent=False, _last_result=None,
            history=SimpleNamespace(history=[]))
        self._orch = SimpleNamespace(agents={"main": agent}, usage_tracker=None)
        self.session_manager = SimpleNamespace(
            get_session_info=lambda sid: ({"user_id": owner} if known else None))

    async def _resolve_or_recreate(self, session_id, info):
        return self._orch

    async def create_session(self, *, user_id, request, **kw):
        self.created.append(request)
        return {"id": "cold-session"}

    async def run_session(self, user_id, session_id):
        self.ran.append(session_id)
        return "Session completed successfully"

    def get_orchestrator(self, session_id):
        return self._orch

    def _extract_chat_reply(self, session_id):
        return ""


@pytest.mark.asyncio
async def test_a_resumable_goal_reuses_its_own_session():
    agent = _ResumingAgent()
    out = await run_task_to_outcome(
        agent, user_id="rob",
        request={"task": "carry on", "resume_session_id": "sess-15",
                 "resume_note": "you were pre-empted at step 7"},
        autonomous=True, goal_id="g1", creator="goal")

    assert agent.ran == ["sess-15"] and agent.created == [], \
        "no cold session is minted when the old one is still recoverable"
    assert out.session_id == "sess-15"
    assert len(agent.pushed) == 1
    assert "restart-note" in str(agent.pushed[0].content)


@pytest.mark.asyncio
async def test_an_unrecoverable_session_falls_back_to_a_cold_run():
    """Not resumable must never FAIL the goal — the note is in the body too."""
    agent = _ResumingAgent(known=False)
    out = await run_task_to_outcome(
        agent, user_id="rob",
        request={"task": "carry on", "resume_session_id": "sess-gone"},
        autonomous=True, goal_id="g1", creator="goal")
    assert out.session_id == "cold-session" and agent.ran == ["cold-session"]


@pytest.mark.asyncio
async def test_another_tenants_session_is_never_resumed():
    agent = _ResumingAgent(owner="someone-else")
    out = await run_task_to_outcome(
        agent, user_id="rob",
        request={"task": "carry on", "resume_session_id": "sess-15"},
        autonomous=True, goal_id="g1", creator="goal")
    assert out.session_id == "cold-session", "a foreign session is not ours to resume"


@pytest.mark.asyncio
async def test_no_resume_key_leaves_the_legacy_path_untouched():
    agent = _ResumingAgent()
    out = await run_task_to_outcome(
        agent, user_id="rob", request={"task": "fresh work"},
        autonomous=True, goal_id="g1", creator="goal")
    assert out.session_id == "cold-session" and agent.pushed == []


# --- the dispatcher half: who asks for a resume, and when ---------------------

class _CapturingAgent:
    """Records which session the dispatcher's run actually entered."""

    def __init__(self):
        self.requests = []
        self.ran = []
        self._orch = SimpleNamespace(agents={}, usage_tracker=None, controller=None)
        self.session_manager = SimpleNamespace(
            get_session_info=lambda sid: {"user_id": "rob"})

    async def _resolve_or_recreate(self, session_id, info):
        return self._orch

    async def create_session(self, *, user_id, request, **kw):
        self.requests.append(dict(request))
        return {"id": "sess-new"}

    async def run_session(self, user_id, session_id):
        self.ran.append(session_id)
        return "Session completed successfully"

    def get_orchestrator(self, session_id):
        return self._orch

    def _extract_chat_reply(self, session_id):
        return ""


class _NoteBoard:
    def __init__(self, goal):
        self.goal = goal
        self.merges = []
        self.failures = []

    def get(self, gid):
        return self.goal

    def merge_payload(self, gid, updates):
        self.merges.append(updates)
        self.goal.payload = {**(self.goal.payload or {}), **updates}
        return True

    def record_failure(self, gid, error=None, session_id=None):
        self.failures.append((gid, error))
        return self.goal

    def record_success(self, gid, session_id=None, result=None):
        return None

    def set_outcome(self, gid, outcome):
        return True

    def stamp_block_kind(self, *a, **kw):
        return True

    def create_ask(self, **kw):
        return None


def _yielded_goal():
    return Goal(id="g-resume", user_id="rob", title="Round 15",
                session_id="sess-15",
                payload={"tools": ["filesystem"],
                         "resume_note": "pre-empted at step 7"})


@pytest.mark.asyncio
async def test_the_dispatcher_asks_to_resume_only_under_the_flag(monkeypatch):
    from agents.task.goals.dispatcher import GoalDispatcher
    monkeypatch.setenv("GOAL_RESUME_SAME_SESSION", "true")
    goal = _yielded_goal()
    agent = _CapturingAgent()
    disp = GoalDispatcher(_NoteBoard(goal), agent)

    await disp._run_goal(goal)

    assert agent.ran == ["sess-15"], "the goal re-entered its own session"
    assert agent.requests == [], "no cold session was minted"
    assert {"resume_note": None} in disp.board.merges, "the note is consumed once"


@pytest.mark.asyncio
async def test_with_the_flag_off_a_yielded_goal_still_starts_cold(monkeypatch):
    from agents.task.goals.dispatcher import GoalDispatcher
    monkeypatch.delenv("GOAL_RESUME_SAME_SESSION", raising=False)
    goal = _yielded_goal()
    agent = _CapturingAgent()
    disp = GoalDispatcher(_NoteBoard(goal), agent)

    await disp._run_goal(goal)

    assert agent.ran == ["sess-new"], "default OFF is byte-identical"
    assert "RESTART NOTE" in agent.requests[0]["task"], \
        "but the restart note still leads the task body"


@pytest.mark.asyncio
async def test_the_note_is_consumed_so_a_later_run_is_not_misled(monkeypatch):
    """A note left on the payload would tell an un-pre-empted run it was cut short."""
    from agents.task.goals.dispatcher import GoalDispatcher
    monkeypatch.delenv("GOAL_RESUME_SAME_SESSION", raising=False)
    goal = _yielded_goal()
    disp = GoalDispatcher(_NoteBoard(goal), _CapturingAgent())

    await disp._run_goal(goal)

    assert not (goal.payload or {}).get("resume_note")


@pytest.mark.asyncio
async def test_a_real_resume_is_recorded_as_a_resumed_event(tmp_path, monkeypatch):
    """`/status work` must render `resumed xM` from a fact, not from intent."""
    import core.event_log as el
    monkeypatch.setattr(el, "_INSTANCES", {})
    log = el.TelemetryEventLog(str(tmp_path / "te.db"))
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: log)

    agent = _ResumingAgent()
    await run_task_to_outcome(
        agent, user_id="rob",
        request={"task": "carry on", "resume_session_id": "sess-15"},
        autonomous=True, goal_id="g1", creator="goal")

    rows = [r for r in log.query(kind="goal_run")
            if r["attrs"].get("outcome") == "resumed"]
    assert len(rows) == 1 and rows[0]["session_id"] == "sess-15"


@pytest.mark.asyncio
async def test_a_failed_resume_records_nothing(tmp_path, monkeypatch):
    import core.event_log as el
    monkeypatch.setattr(el, "_INSTANCES", {})
    log = el.TelemetryEventLog(str(tmp_path / "te.db"))
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: log)

    agent = _ResumingAgent(known=False)
    await run_task_to_outcome(
        agent, user_id="rob",
        request={"task": "carry on", "resume_session_id": "sess-gone"},
        autonomous=True, goal_id="g1", creator="goal")

    assert [r for r in log.query(kind="goal_run")
            if r["attrs"].get("outcome") == "resumed"] == []
