import asyncio
from pathlib import Path

import pytest

from agents.task.goals.board import GoalBoard, STATUS_BLOCKED, STATUS_WAITING
from agents.task.goals.dispatcher import GoalDispatcher
from agents.task.goals.planner import (
    _is_live_waiting_goal, build_planner_prompt, list_deliverables,
)


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "g.db"))


def _seed(board, tmp_path):
    o = board.create_objective(user_id="rob", title="Grow the substack",
                               body="1 real post/day; X is distribution.")
    done = board.create(user_id="rob", title="Write welcome post draft entirely")
    board.claim(done.id, "w", ttl_seconds=60)
    board.record_success(done.id, result="ok\nOUTCOME: project/welcome.md")
    board.set_outcome(done.id, "project/welcome.md")
    blocked = board.create(user_id="rob", title="Broken goal wholly unrelated")
    # NB: after T2's guards record_failure only acts on RUNNING rows — re-claim
    # between failures (first failure returns the goal to 'ready').
    board.claim(blocked.id, "w", ttl_seconds=60)
    board.record_failure(blocked.id, error="tool starved")
    board.claim(blocked.id, "w", ttl_seconds=60)
    board.record_failure(blocked.id, error="tool starved")  # trips breaker (max 2)
    d = tmp_path / "proj"; d.mkdir()
    (d / "welcome.md").write_text("# Welcome post\nbody")
    return o, done, blocked, d


def test_prompt_sections(board, tmp_path):
    o, done, blocked, d = _seed(board, tmp_path)
    p = build_planner_prompt(board, "rob", d)
    assert "Grow the substack" in p                      # objective + body
    assert "1 real post/day" in p
    assert "Write welcome post draft entirely" in p      # done history
    assert "project/welcome.md" in p                     # outcome note
    assert "Broken goal wholly unrelated" in p           # blocked w/ error
    assert "tool starved" in p
    assert "welcome.md" in p and "# Welcome post" not in p  # listing has heading text
    assert "Welcome post" in p
    assert "objective_id" in p and "acceptance" in p and "tools" in p
    # T5: the social ceiling is now derived (default 1), rendered as a digit,
    # not the literal word "ONE" the prompt used to hardcode.
    assert "at most 1 goal" in p.lower()


def test_prompt_anti_paralysis_floor(board, tmp_path):
    # §7.4: the prompt must NOT tell the model to terminate with "create NOTHING";
    # if nothing advances the objective it must surface the concrete blocker instead.
    o, done, blocked, d = _seed(board, tmp_path)
    p = build_planner_prompt(board, "rob", d)
    assert "create NOTHING and say so" not in p
    low = p.lower()
    assert "blocker" in low or "what's blocking" in low or "what is blocking" in low


def test_prompt_distinguishes_routine_empty_from_blocked(board, tmp_path):
    # §7.4 fix: a routine already-covered/empty queue must NOT be reported as a blocker
    # (the ops loop caught the planner crying "blocked" on an empty queue). The prompt
    # must explicitly permit "nothing to add right now" as a NON-blocker outcome.
    o, done, blocked, d = _seed(board, tmp_path)
    p = build_planner_prompt(board, "rob", d).lower()
    assert "nothing to add" in p
    assert "not a blocker" in p or "not report" in p or "do not report" in p


def test_prompt_states_closed_acceptance_check_type_set(board, tmp_path):
    # Proposal 016 #2: the prompt states the EXACT valid check types (closed set)
    # and forbids inventing others — an invented type fail-closes forever.
    o, done, blocked, d = _seed(board, tmp_path)
    p = build_planner_prompt(board, "rob", d)
    assert "artifact_glob" in p and "http_ok" in p and "file_contains" in p
    low = p.lower()
    assert "do not invent" in low or "not invent other types" in low


def test_prompt_surfaces_objective_success_criteria(board, tmp_path):
    # §7.3: an objective's success_criteria (stored in its payload) is shown so the
    # planner measures against what the owner wants, not a self-set proxy.
    o = board.create_objective(user_id="rob", title="Grow X",
                               body="original posts + threads",
                               payload={"success_criteria": "500 followers by Q3"})
    p = build_planner_prompt(board, "rob", None)
    assert "500 followers by Q3" in p


# --- T2.1 Task 4: planner sequencing guidance -------------------------------

def test_prompt_encourages_depends_on_sequencing(board, tmp_path):
    o, done, blocked, d = _seed(board, tmp_path)
    p = build_planner_prompt(board, "rob", d)
    assert "depends_on" in p
    assert "mega-goal" in p


def test_prompt_notes_waiting_goals_excluded_from_ready_ceiling(board, tmp_path):
    o, done, blocked, d = _seed(board, tmp_path)
    p = build_planner_prompt(board, "rob", d)
    low = p.lower()
    assert "5 ready goals" in low
    assert "does not count toward this ceiling" in low


def test_prompt_lists_waiting_goals_with_their_dependency(board, tmp_path):
    o, done, blocked, d = _seed(board, tmp_path)
    dep = board.create(user_id="rob", title="prerequisite for waiting goal")
    board.create(user_id="rob", title="a goal waiting on a prerequisite", depends_on=[dep.id])
    p = build_planner_prompt(board, "rob", d)
    assert "WAITING ON DEPENDENCIES" in p
    assert "a goal waiting on a prerequisite" in p


# --- T2.1 final-review Fix 4: diamond false-negative ------------------------

def test_is_live_waiting_goal_true_for_fully_live_diamond(board):
    """A shared prerequisite reached via two branches must not be misread as
    a cycle. Cycles are impossible at write time (add_dependencies rejects
    them), so `dep_id in _visited` on a still-in-progress live traversal can
    only mean the shared node was already proven live by the branch that hit
    it first — the pre-fix `return False` on revisit was a false negative
    that could sink an otherwise fully-live diamond's leaf.

    Shape: e (ready) <- d (waiting on e) <- {b1, b2} (both waiting on d) <-
    leaf (waiting on b1 AND b2). d is only reached via a `waiting` edge (so
    it gets memoized in `_visited`) from BOTH b1 and b2, reproducing the
    revisit exactly.
    """
    e = board.create(user_id="u1", title="live root")  # ready — genuinely live
    d = board.create(user_id="u1", title="shared waiting prereq", depends_on=[e.id])
    assert d.status == STATUS_WAITING
    # force=True: "branch1"/"branch2" trigram-overlap enough to trip the
    # near-duplicate dedup guard — irrelevant to what this test exercises.
    b1 = board.create(user_id="u1", title="branch1", depends_on=[d.id], force=True)
    b2 = board.create(user_id="u1", title="branch2", depends_on=[d.id], force=True)
    leaf = board.create(user_id="u1", title="leaf", depends_on=[b1.id, b2.id])
    assert leaf.status == STATUS_WAITING

    assert _is_live_waiting_goal(board, leaf.id) is True


def test_is_live_waiting_goal_false_when_diamond_shared_prereq_is_dead(board):
    """Sanity check on the OTHER side: a genuinely dead shared prerequisite
    must still read as dead through a diamond (the fix must not turn EVERY
    revisit into a false positive)."""
    dead = board.create(user_id="u1", title="dead root", max_retries=1)
    board.claim(dead.id, "w", ttl_seconds=900)
    board.record_failure(dead.id, error="boom")  # trips breaker -> blocked
    assert board.get(dead.id).status == STATUS_BLOCKED

    d = board.create(user_id="u1", title="shared waiting prereq", depends_on=[dead.id])
    assert d.status == STATUS_BLOCKED  # creation-time terminal-bad-dep closure
    b1 = board.create(user_id="u1", title="branch1", depends_on=[d.id], force=True)
    b2 = board.create(user_id="u1", title="branch2", depends_on=[d.id], force=True)
    leaf = board.create(user_id="u1", title="leaf", depends_on=[b1.id, b2.id])
    assert leaf.status == STATUS_WAITING

    assert _is_live_waiting_goal(board, leaf.id) is False


def test_list_deliverables(tmp_path):
    d = tmp_path / "proj"; (d / "sub").mkdir(parents=True)
    (d / "a.md").write_text("# Alpha doc\nx")
    (d / "sub" / "b.md").write_text("no heading here")
    (d / ".hidden.md").write_text("# hid")
    out = list_deliverables(d)
    names = {e["name"] for e in out}
    assert "a.md" in names and "sub/b.md" in names and ".hidden.md" not in names
    a = next(e for e in out if e["name"] == "a.md")
    assert a["heading"] == "Alpha doc"


def test_planner_cooldown_persistence(board):
    assert board.last_planner_run_at() is None
    board.mark_planner_run()
    assert board.last_planner_run_at() is not None


class _RecordingAgent:
    def __init__(self):
        self.requests = []

    async def create_session(self, user_id, request):
        self.requests.append(request)
        return {"id": f"plan-sess-{len(self.requests)}"}

    async def run_session(self, user_id, session_id):
        return "Session completed successfully"


def _dispatcher(board, agent, monkeypatch, **env):
    defaults = {"GOALS_ENABLED": "true", "GOAL_PLANNER_ENABLED": "true",
                "GOAL_DAILY_QUOTA": "6"}
    defaults.update(env)
    for k, v in defaults.items():
        monkeypatch.setenv(k, v)
    return GoalDispatcher(board, agent)


def test_planner_fires_when_conditions_met(board, monkeypatch):
    board.create_objective(user_id="rob", title="Grow the substack")
    agent = _RecordingAgent()
    d = _dispatcher(board, agent, monkeypatch)
    asyncio.run(d.dispatch_once())
    asyncio.run(asyncio.sleep(0.05))  # let fire-and-forget task run
    assert any("STANDING OBJECTIVES" in r.get("task", "").upper() or
               "OBJECTIVE" in r.get("task", "") for r in agent.requests)
    # FIX 5: planner bookkeeping is keyed to the tenant the run served.
    assert board.last_planner_run_at(user_id="rob") is not None


def test_planner_skips_without_active_objective(board, monkeypatch):
    agent = _RecordingAgent()
    d = _dispatcher(board, agent, monkeypatch)
    asyncio.run(d.dispatch_once())
    asyncio.run(asyncio.sleep(0.05))
    assert agent.requests == []


def test_planner_respects_cooldown(board, monkeypatch):
    board.create_objective(user_id="rob", title="Grow the substack")
    board.mark_planner_run()  # just ran
    agent = _RecordingAgent()
    d = _dispatcher(board, agent, monkeypatch)
    asyncio.run(d.dispatch_once())
    asyncio.run(asyncio.sleep(0.05))
    assert agent.requests == []


def test_planner_skips_when_ready_queue_healthy(board, monkeypatch):
    # min_ready default is 2 — seed 2 ready goals (queue "healthy") and call
    # _maybe_plan directly so the min-ready gate itself is exercised, rather
    # than relying on dispatch_once's GOAL_MAX_CONCURRENT=0 short-circuit
    # (which returns before _maybe_plan runs and never tests this gate).
    board.create_objective(user_id="rob", title="Grow the substack")
    board.create(user_id="rob", title="Ready goal one entirely")
    board.create(user_id="rob", title="Another distinct ready goal wholly")
    agent = _RecordingAgent()
    d = _dispatcher(board, agent, monkeypatch)
    asyncio.run(d._maybe_plan(headroom_after=5))
    asyncio.run(asyncio.sleep(0.05))
    assert agent.requests == []
    assert board.last_planner_run_at() is None


def test_planner_fires_when_ready_queue_below_min(board, monkeypatch):
    # Below GOAL_PLANNER_MIN_READY (default 2) — only 1 ready goal — the
    # planner SHOULD fire.
    board.create_objective(user_id="rob", title="Grow the substack")
    board.create(user_id="rob", title="Only one ready goal entirely")
    agent = _RecordingAgent()
    d = _dispatcher(board, agent, monkeypatch)
    asyncio.run(d._maybe_plan(headroom_after=5))
    asyncio.run(asyncio.sleep(0.05))
    assert any("STANDING OBJECTIVES" in r.get("task", "").upper() or
               "OBJECTIVE" in r.get("task", "") for r in agent.requests)
    assert board.last_planner_run_at(user_id="rob") is not None


class _Sink:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        return True


class _Container:
    def __init__(self, sink):
        self._sink = sink

    def get_service(self, name):
        return self._sink if name in ("telegram_sink", "message_router") else None


class _EscalatingAgent(_RecordingAgent):
    """Recording agent with a container so push_owner_message can reach a sink."""

    def __init__(self, sink):
        super().__init__()
        self.container = _Container(sink)


def test_planner_stall_escalates_once(board, monkeypatch):
    # §7.2 tail: ready==0 persisting past N planner runs must surface ONE owner
    # escalation (the dead build_empty_pipeline_escalation finally gets a caller),
    # and must not re-ping on every subsequent stalled run.
    board.create_objective(user_id="rob", title="Grow the substack")
    sink = _Sink()
    agent = _EscalatingAgent(sink)
    d = _dispatcher(board, agent, monkeypatch,
                    GOAL_BLOCKER_ESCALATION="true",
                    GOAL_EMPTY_PIPELINE_ESCALATE_AFTER="2")
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "28436760")
    asyncio.run(d._run_planner("rob"))
    assert sink.sent == []  # first stalled run: below threshold
    asyncio.run(d._run_planner("rob"))
    assert len(sink.sent) == 1  # second stalled run: escalate
    assert "pipeline is empty" in sink.sent[0][1] or "Grow the substack" in sink.sent[0][1]
    asyncio.run(d._run_planner("rob"))
    assert len(sink.sent) == 1  # escalate ONCE, not every run


def test_planner_stall_counter_resets_when_board_refills(board, monkeypatch):
    board.create_objective(user_id="rob", title="Grow the substack")
    sink = _Sink()
    agent = _EscalatingAgent(sink)
    d = _dispatcher(board, agent, monkeypatch,
                    GOAL_BLOCKER_ESCALATION="true",
                    GOAL_EMPTY_PIPELINE_ESCALATE_AFTER="2")
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "28436760")
    asyncio.run(d._run_planner("rob"))
    board.create(user_id="rob", title="A fresh ready goal appeared entirely")
    asyncio.run(d._run_planner("rob"))  # board non-empty -> reset, no escalation
    assert sink.sent == []


def test_planner_disabled_by_default(board, monkeypatch):
    board.create_objective(user_id="rob", title="Grow the substack")
    agent = _RecordingAgent()
    d = _dispatcher(board, agent, monkeypatch, GOAL_PLANNER_ENABLED="false")
    asyncio.run(d.dispatch_once())
    asyncio.run(asyncio.sleep(0.05))
    assert agent.requests == []
