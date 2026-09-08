"""Empty-pipeline escalation honesty + planner backoff (2026-08-29 "no goals"
forensics, C3/C4/C5/C6/C8).

Prod sent the owner "🫗 My goal pipeline is empty — I have nothing queued for
Promote POLYROB…" twice in five hours while the trading stream had run ten clean
cycles: the check read only ``board.ready(limit=1)``, the once-per-stall flag was
process memory (14 restarts in 36 h), and the message named the first active
objective by priority — a budget-spent one.
"""
import asyncio
import time

import pytest
import yaml

from agents.task.goals.board import GoalBoard
from agents.task.goals.dispatcher import GoalDispatcher
from agents.task.goals.planner import build_planner_prompt, planner_backoff_multiplier


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "g.db"))


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


class _Agent:
    """Plans nothing: every planner session ends with a REAL BLOCKER summary."""

    def __init__(self, sink, summary="REAL BLOCKER: owner decision needed"):
        self.container = _Container(sink)
        self.requests = []
        self.summary = summary

    async def create_session(self, user_id, request):
        self.requests.append(request)
        return {"id": f"plan-sess-{len(self.requests)}"}

    async def run_session(self, user_id, session_id):
        return self.summary


def _dispatcher(board, agent, monkeypatch, **env):
    defaults = {"GOALS_ENABLED": "true", "GOAL_PLANNER_ENABLED": "true",
                "GOAL_BLOCKER_ESCALATION": "true",
                "GOAL_EMPTY_PIPELINE_ESCALATE_AFTER": "2",
                "POLYROB_OWNER_TELEGRAM_ID": "28436760",
                "POLYROB_STREAMS_MANIFEST": ""}
    defaults.update(env)
    for k, v in defaults.items():
        monkeypatch.setenv(k, v)
    return GoalDispatcher(board, agent)


def _plan_twice(d):
    asyncio.run(d._run_planner("rob"))
    asyncio.run(d._run_planner("rob"))


# --- C5: durable once-per-stall -----------------------------------------------

def test_stall_escalation_survives_a_dispatcher_restart(board, monkeypatch):
    board.create_objective(user_id="rob", title="Grow the substack")
    sink = _Sink()
    d = _dispatcher(board, _Agent(sink), monkeypatch)
    _plan_twice(d)
    assert len(sink.sent) == 1

    restarted = GoalDispatcher(board, _Agent(sink))  # new process, same board
    asyncio.run(restarted._run_planner("rob"))
    asyncio.run(restarted._run_planner("rob"))
    assert len(sink.sent) == 1  # still the same stall: no second push


def test_a_refill_ends_the_stall_and_a_later_stall_escalates_again(board, monkeypatch):
    board.create_objective(user_id="rob", title="Grow the substack")
    sink = _Sink()
    d = _dispatcher(board, _Agent(sink), monkeypatch)
    _plan_twice(d)
    assert len(sink.sent) == 1
    g = board.create(user_id="rob", title="fresh ready goal entirely", force=True)
    asyncio.run(d._run_planner("rob"))  # board has work: streak ends
    board.cancel(g.id, user_id="rob")
    d.task_agent.summary = "REAL BLOCKER: a different decision now"  # new stall, new word
    _plan_twice(d)                      # a NEW stall
    assert len(sink.sent) == 2


# --- C4: in-flight legs and idle streams are not a stall -----------------------

def test_running_or_waiting_work_is_not_an_empty_pipeline(board, monkeypatch):
    board.create_objective(user_id="rob", title="Grow the substack")
    head = board.create(user_id="rob", title="head leg of a cycle", force=True)
    board.create(user_id="rob", title="tail leg of a cycle", force=True, depends_on=[head.id])
    board.claim(head.id, "w", ttl_seconds=60)  # head running, tail waiting, 0 ready
    sink = _Sink()
    d = _dispatcher(board, _Agent(sink), monkeypatch)
    _plan_twice(d)
    assert sink.sent == []


def _manifest(tmp_path, cadence_hours=4):
    doc = {"version": 1, "streams": [{
        "id": "treasury-trading", "cadence_hours": cadence_hours, "max_live_goals": 1,
        "objective": {"title": "Grow the treasury", "body": "trade"},
        "goals": [{"title": "Treasury: refresh the watchlist", "body": "x",
                   "tools": ["defi_data", "task"]}],
    }]}
    p = tmp_path / "streams.yaml"
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return str(p)


def test_a_stream_idle_between_cycles_is_not_a_stall(board, tmp_path, monkeypatch):
    from agents.task.goals import streams as S
    path = _manifest(tmp_path)
    stream = S.load_manifest(path)[0]
    oid, _ = S.ensure_objective(board, "rob", stream)
    for g in S.seed_stream(board, "rob", stream, oid):
        board.claim(g.id, "w", ttl_seconds=60)
        board.record_success(g.id, result="ok")  # the cycle finished 1 min ago
    sink = _Sink()
    d = _dispatcher(board, _Agent(sink), monkeypatch, POLYROB_STREAMS_MANIFEST=path)
    _plan_twice(d)
    assert sink.sent == []


def test_a_stream_whose_window_has_lapsed_does_not_suppress_the_stall(board, tmp_path,
                                                                     monkeypatch):
    """The seeder should have refilled the board and did not — that IS a stall."""
    from agents.task.goals import streams as S
    path = _manifest(tmp_path)
    stream = S.load_manifest(path)[0]
    old = GoalBoard(board.db_path, clock=lambda: time.time() - 9 * 3600)
    oid, _ = S.ensure_objective(old, "rob", stream)
    for g in S.seed_stream(old, "rob", stream, oid):
        old.claim(g.id, "w", ttl_seconds=60)
        old.record_success(g.id, result="ok")
    sink = _Sink()
    d = _dispatcher(board, _Agent(sink), monkeypatch, POLYROB_STREAMS_MANIFEST=path)
    _plan_twice(d)
    assert len(sink.sent) == 1


# --- C3: budgets spent with open asks is covered, not blocked --------------------

def _finish(board, goal):
    board.claim(goal.id, "w", ttl_seconds=60)
    board.record_success(goal.id, result="ok")


def test_every_objective_spent_with_an_open_ask_is_covered_not_escalated(board, monkeypatch):
    monkeypatch.setenv("OBJECTIVE_GOAL_BUDGET", "1")
    obj = board.create_objective(user_id="rob", title="Promote in public")
    _finish(board, board.create(user_id="rob", title="the only child", parent_id=obj.id,
                                force=True))   # done still counts against the budget
    board.escalate_spent_objectives(user_id="rob")   # durable ask is open
    sink = _Sink()
    d = _dispatcher(board, _Agent(sink), monkeypatch)
    _plan_twice(d)
    assert sink.sent == []


# --- C6: the message names an objective that could take work -------------------

def test_escalation_names_a_servable_objective_not_the_spent_one(board, monkeypatch):
    monkeypatch.setenv("OBJECTIVE_GOAL_BUDGET", "1")
    spent = board.create_objective(user_id="rob", title="Promote in public", priority=9)
    _finish(board, board.create(user_id="rob", title="promo child", parent_id=spent.id,
                                force=True))
    board.escalate_spent_objectives(user_id="rob")
    board.create_objective(user_id="rob", title="Ship real software", priority=1)
    sink = _Sink()
    d = _dispatcher(board, _Agent(sink), monkeypatch)
    _plan_twice(d)
    assert len(sink.sent) == 1
    text = sink.sent[0][1]
    assert "Ship real software" in text
    assert "Promote in public" not in text.split("My planner's last word")[0]


# --- C8: planner backoff after consecutive empty runs ---------------------------

def test_backoff_multiplier_grows_with_empty_runs_and_caps():
    assert planner_backoff_multiplier(0) == 1
    assert planner_backoff_multiplier(1) == 1
    assert planner_backoff_multiplier(2) == 2
    assert planner_backoff_multiplier(3) == 4
    assert planner_backoff_multiplier(9) == 4


def test_planner_run_records_its_outcome_on_the_board(board, monkeypatch):
    board.create_objective(user_id="rob", title="Grow the substack")
    d = _dispatcher(board, _Agent(_Sink()), monkeypatch)
    asyncio.run(d._run_planner("rob"))
    # FIX 5: the outcome is filed under the tenant the planner ran for.
    assert board.consecutive_empty_planner_runs(user_id="rob") == 1


def test_maybe_plan_waits_out_the_backoff_after_empty_runs(board, monkeypatch):
    board.create_objective(user_id="rob", title="Grow the substack")
    agent = _Agent(_Sink())
    d = _dispatcher(board, agent, monkeypatch, GOAL_PLANNER_COOLDOWN_SEC="100")
    clock = {"t": time.time()}
    b = GoalBoard(board.db_path, clock=lambda: clock["t"])
    d.board = b
    for _ in range(3):
        b.mark_planner_outcome(queued=0)   # three empty runs -> 4x cooldown
    b.mark_planner_run()
    clock["t"] += 250                      # past 1x cooldown, inside 4x
    asyncio.run(d._maybe_plan(headroom_after=5))
    asyncio.run(asyncio.sleep(0.05))
    assert agent.requests == []
    clock["t"] += 200                      # past 4x cooldown
    asyncio.run(d._maybe_plan(headroom_after=5))
    asyncio.run(asyncio.sleep(0.05))
    assert len(agent.requests) == 1


# --- C9: the board's open asks are the only live asks --------------------------

def test_prompt_lists_open_asks_as_the_only_live_blockers(board, tmp_path):
    board.create_objective(user_id="rob", title="Grow the substack")
    board.create_ask(user_id="rob", what="Fund the Solana wallet", why="no SOL")
    p = build_planner_prompt(board, "rob", tmp_path)
    assert "OPEN ASKS" in p
    assert "Fund the Solana wallet" in p
    low = p.lower()
    assert "not listed here" in low and "resolved" in low


def test_prompt_says_no_ask_is_open_when_the_board_has_none(board, tmp_path):
    board.create_objective(user_id="rob", title="Grow the substack")
    p = build_planner_prompt(board, "rob", tmp_path)
    assert "OPEN ASKS" in p and "none" in p.split("OPEN ASKS")[1][:120].lower()
