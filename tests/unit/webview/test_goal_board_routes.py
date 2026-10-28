"""Work › Goals readers (``webview/goal_board_routes.py``) and the create
route's ``depends_on``, over a REAL ``goals.db`` (``goal_board_seed``).

What must hold: every live goal of the tenant and none of another's; the
dependency edges, the decision that holds a goal and the per-run limit ride on
the board; a failed read is ``error`` (never an empty board) and a failed side
read is NAMED; ``GoalBoard.list`` (the dispatcher's claim queue) never backs
the view; the detail is tenant-scoped and carries the runs from the event log.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.unit.webview.goal_board_seed import OWNER, seed


def _client(monkeypatch, tmp_path, user_id=OWNER):
    monkeypatch.setenv("POLYROB_POSTURE", "local")
    import webview.activity as activity
    import webview.goal_board_routes as routes
    import webview.pages as pages
    import webview.pages_new as pages_new
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: user_id)
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(pages.AutonomyConfig, "goals_enabled", staticmethod(lambda: True))
    monkeypatch.setattr(activity, "_sessions_data_root", lambda: str(tmp_path))
    monkeypatch.setattr(activity, "_data_dir", lambda: str(tmp_path))
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "telemetry_events.db"))
    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(pages_new.router)
    app.include_router(pages.router)
    return TestClient(app)


@pytest.fixture
def seeded(tmp_path):
    return seed(str(tmp_path / "goals.db"))


def _by_id(rows):
    return {r["id"]: r for r in rows}


def test_board_lists_every_live_goal_of_the_tenant(monkeypatch, tmp_path, seeded):
    body = _client(monkeypatch, tmp_path).get("/api/webgate/goals/board").json()
    assert "error" not in body
    assert body["columns"] == ["triage", "waiting", "ready", "running", "blocked", "paused"]
    goals = _by_id(body["goals"])
    assert {g["status"] for g in goals.values()} == set(body["columns"])
    assert seeded["foreign"] not in goals                       # another tenant's goal
    assert {seeded["shipped"], seeded["dropped"]} == {g["id"] for g in body["finished"]}
    assert body["counts"]["done"] == 1 and body["counts"]["cancelled"] == 1
    # the dependency, both ways
    assert goals[seeded["recap"]]["depends_on"] == [seeded["digest"]]
    assert goals[seeded["digest"]]["blocks"] == [seeded["recap"]]
    # the open decision and the goal it holds
    assert [a["id"] for a in body["asks"]] == [seeded["ask"]]
    assert body["asks"][0]["holds"] == [seeded["site"]]
    assert goals[seeded["site"]]["held_by"] == [seeded["ask"]]
    # who started it, the cost against the per-run limit, the sub-goal's parent
    assert goals[seeded["prices"]]["origin"] == "owner"
    assert goals[seeded["digest"]]["origin"] == "granted"
    assert goals[seeded["digest"]]["origin_name"] == "weekly-digest"
    assert goals[seeded["sorting"]]["origin"] == "self"
    assert goals[seeded["prices"]]["cost_usd"] == 0.42
    assert goals[seeded["trade"]]["cost_usd"] is None           # not recorded, not $0
    assert body["limits"]["run_usd"] > 0 and body["limits"]["max_steps"] >= 1
    assert goals[seeded["sub"]]["parent_id"] == seeded["digest"]
    assert all(g["last_activity"] for g in goals.values())
    assert all(g["status_reason"] for g in goals.values())
    assert body["unreadable"] == {}


def test_board_never_reads_the_dispatch_queue(monkeypatch, tmp_path, seeded):
    from agents.task.goals.board import GoalBoard

    def forbidden(self, *a, **k):
        raise AssertionError("GoalBoard.list() must never back a view")
    monkeypatch.setattr(GoalBoard, "list", forbidden)
    r = _client(monkeypatch, tmp_path).get("/api/webgate/goals/board")
    assert r.status_code == 200 and "error" not in r.json()


def test_another_tenant_sees_only_its_own(monkeypatch, tmp_path, seeded):
    body = _client(monkeypatch, tmp_path, user_id="u2").get("/api/webgate/goals/board").json()
    assert [g["id"] for g in body["goals"]] == [seeded["foreign"]]
    assert body["asks"] == [] and body["finished"] == []


def test_unreadable_board_is_an_error_not_an_empty_board(monkeypatch, tmp_path):
    import webview.pages as pages

    class Broken:
        def list_recent(self, **k):
            raise RuntimeError("goals.db is locked")
    client = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(pages, "_webgate_goal_board", lambda: Broken())
    r = client.get("/api/webgate/goals/board")
    assert r.status_code == 200
    assert "goals.db is locked" in r.json()["error"]
    assert r.json()["goals"] == []


def test_a_failed_side_read_is_named(monkeypatch, tmp_path, seeded):
    from agents.task.goals.board import GoalBoard

    def broken(self, **k):
        raise RuntimeError("edges unreadable")
    monkeypatch.setattr(GoalBoard, "edges", broken)
    body = _client(monkeypatch, tmp_path).get("/api/webgate/goals/board").json()
    assert "edges unreadable" in body["unreadable"]["dependencies"]
    assert len(body["goals"]) == 7                               # the rest still draws


def test_finished_window_says_when_there_is_more(monkeypatch, tmp_path, seeded):
    client = _client(monkeypatch, tmp_path)
    one = client.get("/api/webgate/goals/board?finished=1").json()
    assert len(one["finished"]) == 1 and one["more_finished"] is True
    none = client.get("/api/webgate/goals/board?finished=0").json()
    assert none["finished"] == [] and none["more_finished"] is False


def test_no_show_more_at_the_finished_cap(monkeypatch, tmp_path, seeded):
    import webview.goal_board_routes as gbr
    monkeypatch.setattr(gbr, "FINISHED_MAX", 1)
    body = _client(monkeypatch, tmp_path).get("/api/webgate/goals/board?finished=5").json()
    assert len(body["finished"]) == 1 and body["more_finished"] is False


def test_events_last_reads_only_the_newest_rows(tmp_path):
    from agents.task.goals.board import GoalBoard
    board = GoalBoard(str(tmp_path / "goals.db"))
    g = board.create(user_id="u1", title="t")
    for i in range(5):
        board._event(g.id, "note", {"i": i})
    every = board.events(g.id)
    assert len(every) >= 5
    assert board.events(g.id, last=2) == every[-2:]


def test_detail_carries_the_whole_record(monkeypatch, tmp_path, seeded):
    from core.event_log import TelemetryEventLog
    log = TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    log.record("goal_run", user_id=OWNER, session_id="s-digest", source="goal",
               goal_id=seeded["digest"], outcome="done", steps=12, spend_usd=1.37,
               stop_reason="done")
    log.record("goal_run", user_id=OWNER, source="goal", goal_id=seeded["prices"],
               outcome="failed", steps=3)                        # another goal's run
    log.record("goal_run", user_id="u2", source="goal", goal_id=seeded["digest"],
               outcome="done")                                   # another tenant's row
    client = _client(monkeypatch, tmp_path)

    d = client.get(f"/api/webgate/goals/{seeded['digest']}/detail").json()
    assert d["ok"] is True and d["status"] == "running"
    assert [r["id"] for r in d["waited_on_by"]] == [seeded["recap"]]
    assert d["waited_on_by"][0]["title"] == "Post the weekly recap"
    assert [c["id"] for c in d["sub_goals"]] == [seeded["sub"]]
    assert len(d["runs"]) == 1
    run = d["runs"][0]
    assert (run["steps"], run["spend_usd"], run["session_id"]) == (12, 1.37, "s-digest")
    assert run["stop_text"]
    assert d["sessions"] == ["s-digest"]
    assert d["events"][0]["kind"]                                # newest first
    assert d["unreadable"] == {}

    p = client.get(f"/api/webgate/goals/{seeded['prices']}/detail").json()
    assert p["acceptance"] == "prices.json is under one hour old"
    assert p["body"] == "Read the three price feeds."

    s = client.get(f"/api/webgate/goals/{seeded['site']}/detail").json()
    assert [a["id"] for a in s["decisions"]] == [seeded["ask"]]
    assert s["decisions"][0]["own_place"] is False

    sub = client.get(f"/api/webgate/goals/{seeded['sub']}/detail").json()
    assert sub["parent"]["id"] == seeded["digest"]


@pytest.mark.parametrize("name", ["foreign", "ask"])
def test_detail_is_a_miss_for_a_foreign_goal_or_an_ask(monkeypatch, tmp_path, seeded, name):
    r = _client(monkeypatch, tmp_path).get(f"/api/webgate/goals/{seeded[name]}/detail")
    assert r.status_code == 404 and r.json()["ok"] is False


def test_detail_names_an_unreadable_run_log(monkeypatch, tmp_path, seeded):
    import webview.goal_board_routes as routes

    def broken(*a, **k):
        raise RuntimeError("telemetry locked")
    monkeypatch.setattr(routes, "_runs", broken)
    d = _client(monkeypatch, tmp_path).get(f"/api/webgate/goals/{seeded['digest']}/detail").json()
    assert d["ok"] is True and "telemetry locked" in d["unreadable"]["runs"]


@pytest.mark.parametrize("name,verb,status", [
    ("site", "retry", "ready"), ("site", "stop", "cancelled"), ("trade", "retry", "ready"),
    ("dropped", "drop", "cancelled"), ("recap", "pause", "paused"), ("digest", "stop", "cancelled"),
])
def test_each_card_verb_is_the_existing_goal_verb(monkeypatch, tmp_path, seeded, name, verb, status):
    from agents.task.goals.board import GoalBoard
    client = _client(monkeypatch, tmp_path)
    r = client.post(f"/api/webgate/goals/{seeded[name]}/{verb}")
    assert r.status_code == 200, r.text
    assert GoalBoard(str(tmp_path / "goals.db")).get(seeded[name]).status == status


def test_create_with_depends_on_waits(monkeypatch, tmp_path, seeded):
    from agents.task.goals.board import GoalBoard
    client = _client(monkeypatch, tmp_path)
    r = client.post("/api/webgate/goals", json={"title": "Email the recap",
                                                "depends_on": [seeded["recap"]]})
    assert r.status_code == 201, r.text
    board = GoalBoard(str(tmp_path / "goals.db"))
    assert board.get(r.json()["id"]).status == "waiting"
    assert board.dependencies(r.json()["id"]) == [seeded["recap"]]


def test_create_refuses_a_bad_or_foreign_dependency(monkeypatch, tmp_path, seeded):
    client = _client(monkeypatch, tmp_path)
    shape = client.post("/api/webgate/goals", json={"title": "x", "depends_on": "abc"})
    assert shape.status_code == 400
    foreign = client.post("/api/webgate/goals", json={"title": "y",
                                                      "depends_on": [seeded["foreign"]]})
    assert foreign.status_code == 400


def test_an_ask_on_the_goal_rail_holds_it_on_the_board_and_in_the_detail(
        monkeypatch, tmp_path, seeded):
    """The board and the detail agree: an ask that names a goal only through its
    own ``goal:<id>`` rail holds that goal in both (the detail used to miss it)."""
    from agents.task.goals.board import GoalBoard
    b = GoalBoard(str(tmp_path / "goals.db"))
    q = b.create(user_id=OWNER, title="Queue the newsletter", force=True)
    ask = b.create_ask(user_id=OWNER, what="Which list gets the newsletter?",
                       extra_payload={"rail_id": f"goal:{q.id}"})
    client = _client(monkeypatch, tmp_path)
    card = _by_id(client.get("/api/webgate/goals/board").json()["goals"])[q.id]
    assert card["held_by"] == [ask.id]
    d = client.get(f"/api/webgate/goals/{q.id}/detail").json()
    assert [a["id"] for a in d["decisions"]] == [ask.id]
    assert d["status_reason"] == card["status_reason"]


def test_an_ask_does_not_hold_a_running_goal(monkeypatch, tmp_path, seeded):
    """The ``goal_owner_verbs.reason`` rule: only a ready or blocked goal waits
    on an answer; a running one that an old ask names is still running."""
    from agents.task.goals.board import GoalBoard
    GoalBoard(str(tmp_path / "goals.db")).create_ask(
        user_id=OWNER, what="Shorter digest?", blocks_goal_ids=[seeded["digest"]])
    client = _client(monkeypatch, tmp_path)
    card = _by_id(client.get("/api/webgate/goals/board").json()["goals"])[seeded["digest"]]
    assert card["held_by"] == [] and "answer" not in card["status_reason"]
    d = client.get(f"/api/webgate/goals/{seeded['digest']}/detail").json()
    assert d["decisions"] == []


def test_finished_goals_are_newest_by_when_they_ended(monkeypatch, tmp_path, seeded):
    from agents.task.goals.board import GoalBoard
    b = GoalBoard(str(tmp_path / "goals.db"))
    b.update_status(seeded["trade"], "done")       # created early, finished last
    from core.sqlite_util import execute_retry
    execute_retry(b.db_path, "UPDATE goals SET completed_at=? WHERE id=?",
                  (4_000_000_000.0, seeded["trade"]))
    one = _client(monkeypatch, tmp_path).get("/api/webgate/goals/board?finished=1").json()
    assert [g["id"] for g in one["finished"]] == [seeded["trade"]]


def test_detail_finds_an_old_goals_runs_and_names_an_unreadable_log(
        monkeypatch, tmp_path, seeded):
    from core.event_log import TelemetryEventLog
    log = TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    log.record("goal_run", user_id=OWNER, source="goal", goal_id=seeded["prices"],
               outcome="done", steps=4)
    for i in range(30):                              # later runs of another goal
        log.record("goal_run", user_id=OWNER, source="goal", goal_id=seeded["digest"],
                   outcome="done", steps=i)
    client = _client(monkeypatch, tmp_path)
    d = client.get(f"/api/webgate/goals/{seeded['prices']}/detail").json()
    assert [r["steps"] for r in d["runs"]] == [4]
    assert len(client.get(f"/api/webgate/goals/{seeded['digest']}/detail").json()["runs"]) == 20

    (tmp_path / "telemetry_events.db").write_bytes(b"this is not a database" * 64)
    d = client.get(f"/api/webgate/goals/{seeded['prices']}/detail").json()
    assert d["ok"] is True and "runs" in d["unreadable"] and d["runs"] == []
