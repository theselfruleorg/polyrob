"""Declarative multi-stream seeding."""
import os
import time

import pytest
import yaml

from agents.task.goals.board import GoalBoard
from agents.task.goals import streams as S


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


MANIFEST = {
    "version": 1,
    "streams": [
        {
            "id": "demo-stream",
            "cadence_hours": 4,
            "max_live_goals": 2,
            "objective": {
                "title": "Demo objective",
                "body": "Do the demo work.",
                "priority": 1,
                "success_criteria": "a file exists",
                "goal_budget": 12,
            },
            "goals": [
                {"title": "step one", "body": "do one", "priority": 2,
                 "tools": ["filesystem", "task"], "max_steps": 20},
                {"title": "step two", "body": "do two", "priority": 2,
                 "tools": ["filesystem", "task", "defi_trade"], "max_steps": 30},
            ],
        }
    ],
}


@pytest.fixture
def manifest_path(tmp_path):
    p = tmp_path / "streams.yaml"
    p.write_text(yaml.safe_dump(MANIFEST), encoding="utf-8")
    return str(p)


def test_load_manifest_returns_the_stream_list(manifest_path):
    got = S.load_manifest(manifest_path)
    assert [s["id"] for s in got] == ["demo-stream"]
    assert got[0]["goals"][1]["tools"] == ["filesystem", "task", "defi_trade"]


def test_load_manifest_rejects_a_stream_with_no_goals(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(
        {"version": 1, "streams": [{"id": "x", "objective": {"title": "t"}, "goals": []}]}),
        encoding="utf-8")
    with pytest.raises(ValueError, match="goals"):
        S.load_manifest(str(bad))


def test_load_manifest_rejects_a_duplicate_stream_id(tmp_path):
    one = {"id": "x", "objective": {"title": "t"},
           "goals": [{"title": "a", "body": "b", "tools": ["task"]}]}
    bad = tmp_path / "dup.yaml"
    bad.write_text(yaml.safe_dump({"version": 1, "streams": [one, dict(one)]}),
                   encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        S.load_manifest(str(bad))


def test_load_manifest_refuses_a_session_workspace_path(tmp_path):
    d = tmp_path / "data" / "auto" / "rob" / "sessions" / "s1" / "workspace"
    d.mkdir(parents=True)
    p = d / "streams.yaml"
    p.write_text(yaml.safe_dump(MANIFEST), encoding="utf-8")
    with pytest.raises(ValueError, match="session workspace"):
        S.load_manifest(str(p))


def test_load_manifest_refuses_a_relative_path_into_a_session_workspace(tmp_path):
    """abspath alone normalizes a relative path but the string check still has
    to run on the RESOLVED path, not the one the caller typed — this proves
    the realpath() normalization is load-bearing, not incidental."""
    d = tmp_path / "data" / "auto" / "rob" / "sessions" / "s2" / "workspace"
    d.mkdir(parents=True)
    p = d / "streams.yaml"
    p.write_text(yaml.safe_dump(MANIFEST), encoding="utf-8")
    rel = os.path.relpath(str(p), os.getcwd())
    with pytest.raises(ValueError, match="session workspace"):
        S.load_manifest(rel)


def test_load_manifest_refuses_a_symlink_into_a_session_workspace(tmp_path):
    """A symlink's own path names neither 'sessions' nor 'workspace', but it
    POINTS AT a manifest inside one. abspath() does not follow symlinks while
    open() does -- so only realpath() actually closes this hole."""
    d = tmp_path / "data" / "auto" / "rob" / "sessions" / "s3" / "workspace"
    d.mkdir(parents=True)
    target = d / "streams.yaml"
    target.write_text(yaml.safe_dump(MANIFEST), encoding="utf-8")
    outside_link = tmp_path / "innocuous_name.yaml"
    os.symlink(str(target), str(outside_link))
    with pytest.raises(ValueError, match="session workspace"):
        S.load_manifest(str(outside_link))


def test_load_manifest_raises_valueerror_on_malformed_yaml(tmp_path):
    """yaml.safe_load raises yaml.YAMLError on broken syntax, not ValueError --
    load_manifest must convert it so every caller (the runner's
    `except (OSError, ValueError)` included) gets one exception vocabulary."""
    bad = tmp_path / "malformed.yaml"
    bad.write_text("streams: [\n  - id: x\n", encoding="utf-8")  # unterminated flow sequence
    with pytest.raises(ValueError, match="invalid YAML"):
        S.load_manifest(str(bad))


def test_ensure_objective_creates_then_reuses_by_stream_id(board, manifest_path):
    stream = S.load_manifest(manifest_path)[0]
    oid, note = S.ensure_objective(board, "rob", stream)
    assert "created" in note
    obj = board.get(oid)
    assert obj.kind == "objective" and obj.status == "active"
    assert obj.payload["stream_id"] == "demo-stream"
    assert obj.payload["success_criteria"] == "a file exists"
    assert obj.payload["goal_budget"] == 12

    again, note2 = S.ensure_objective(board, "rob", stream)
    assert again == oid and "already active" in note2


def test_ensure_objective_adopts_a_legacy_objective_by_title(board, manifest_path):
    stream = S.load_manifest(manifest_path)[0]
    legacy = board.create_objective(user_id="rob", title="Demo objective", force=True)
    oid, note = S.ensure_objective(board, "rob", stream)
    assert oid == legacy.id and "adopted" in note
    assert board.get(oid).payload["stream_id"] == "demo-stream"


def test_seed_stream_writes_tools_verbatim_and_chains_the_goals(board, manifest_path):
    stream = S.load_manifest(manifest_path)[0]
    oid, _ = S.ensure_objective(board, "rob", stream)
    seeded = S.seed_stream(board, "rob", stream, oid)

    assert [g.title for g in seeded] == ["step one", "step two"]
    assert seeded[0].status == "ready"
    assert seeded[1].status == "waiting"          # chained behind step one
    assert board.dependencies(seeded[1].id) == [seeded[0].id]
    # The operator grant survives verbatim — no inference, no filtering.
    assert seeded[1].payload["tools"] == ["filesystem", "task", "defi_trade"]
    assert seeded[1].payload["stream"] == "demo-stream"
    assert seeded[1].payload["max_steps"] == 30
    assert seeded[0].parent_id == oid


def test_stream_is_due_blocks_while_goals_are_live(board, manifest_path):
    stream = S.load_manifest(manifest_path)[0]
    oid, _ = S.ensure_objective(board, "rob", stream)
    S.seed_stream(board, "rob", stream, oid)
    due, why = S.stream_is_due(board, "rob", stream, time.time() + 10 * 3600)
    assert due is False and "live" in why


def test_stream_is_due_blocks_while_a_goal_is_blocked(board, manifest_path):
    """A stuck (blocked) leg must still count as live. blocked is NOT terminal
    in POLYROB (a provider_outage requeues itself; anything else ages out to
    cancelled), so if it dropped out of "live" the moment it blocked, a lapsed
    cadence window would green-light a second, overlapping seed of the same
    money-granting stream while the first cycle's blocked leg is still there."""
    stream = S.load_manifest(manifest_path)[0]
    oid, _ = S.ensure_objective(board, "rob", stream)
    seeded = S.seed_stream(board, "rob", stream, oid)
    assert board.block_from_ready(seeded[0].id, error="simulated stall") is True
    assert board.get(seeded[0].id).status == "blocked"
    due, why = S.stream_is_due(board, "rob", stream, time.time() + 10 * 3600)
    assert due is False and "live" in why


def test_stream_is_due_blocks_inside_the_cadence_window(board, manifest_path):
    stream = S.load_manifest(manifest_path)[0]
    oid, _ = S.ensure_objective(board, "rob", stream)
    seeded = S.seed_stream(board, "rob", stream, oid)
    for g in seeded:
        board.cancel(g.id, user_id="rob")          # nothing live any more
    due, why = S.stream_is_due(board, "rob", stream, time.time())
    assert due is False and "cadence" in why


def test_stream_is_due_when_the_window_has_passed(board, manifest_path):
    stream = S.load_manifest(manifest_path)[0]
    oid, _ = S.ensure_objective(board, "rob", stream)
    seeded = S.seed_stream(board, "rob", stream, oid)
    for g in seeded:
        board.cancel(g.id, user_id="rob")
    due, why = S.stream_is_due(board, "rob", stream, time.time() + 5 * 3600)
    assert due is True, why


def test_stream_is_due_treats_an_explicit_zero_max_live_goals_as_disabled(board, manifest_path):
    """`max_live_goals: 0` is a real ceiling meaning "never seed this stream",
    not an unset value that should fall back to len(goals) -- `or` treats 0 as
    falsy and would silently widen the ceiling back up."""
    stream = dict(S.load_manifest(manifest_path)[0])
    stream["max_live_goals"] = 0
    due, why = S.stream_is_due(board, "rob", stream, time.time())
    assert due is False and "ceiling 0" in why


def test_stream_is_due_honors_the_owner_stream_pause(board, manifest_path, monkeypatch):
    """2026-09-02 incident: an owner "stop all ghosts" directive cancelled every
    live goal, but the stream itself kept reseeding on schedule with no
    awareness of the directive. `AutonomyConfig.stream_seeding_paused()` must be
    the FIRST check `stream_is_due` makes, ahead of the live/cadence throttles,
    so a paused stream never reads as "due" for any other reason."""
    monkeypatch.setenv("STREAM_SEEDING_PAUSE", "true")
    stream = S.load_manifest(manifest_path)[0]
    due, why = S.stream_is_due(board, "rob", stream, time.time() + 10 * 3600)
    assert due is False
    assert "paused" in why


def test_merge_payload_preserves_other_keys(board):
    g = board.create(user_id="rob", title="t", payload={"tools": ["task"]}, force=True)
    assert board.merge_payload(g.id, {"stream": "demo"}) is True
    p = board.get(g.id).payload
    assert p["tools"] == ["task"] and p["stream"] == "demo"


def test_the_shipped_manifest_parses():
    """A malformed shipped manifest silently stops every stream — catch it in CI."""
    got = S.load_manifest(S.default_manifest_path())
    assert got, "the shipped manifest declares no streams"
    assert "treasury-trading" in {s["id"] for s in got}


# --- final-review regressions ------------------------------------------------
#
# The three Criticals below were all green on the suite as shipped, because no
# test seeded a SECOND cycle against one objective, padded the board past the
# scan window, or put a legacy-tagged cycle on the board. Each test here fails
# against the pre-fix code.


def _complete(board, goal_id: str) -> None:
    """Take one goal through the real ready -> running -> done lifecycle."""
    assert board.claim(goal_id, "test-worker", ttl_seconds=60) is not None, goal_id
    board.record_success(goal_id, session_id="s", result="ok")


def _two_leg_stream(sid: str = "demo-stream", *, goal_budget: int = 4) -> dict:
    return {
        "id": sid,
        "cadence_hours": 4,
        "max_live_goals": 2,
        "objective": {"title": f"{sid} objective", "body": "standing work",
                      "priority": 1, "goal_budget": goal_budget,
                      "success_criteria": "the cycle keeps running"},
        "goals": [
            {"title": f"{sid} leg one", "body": "do one", "priority": 2,
             "tools": ["filesystem", "task"]},
            {"title": f"{sid} leg two", "body": "do two", "priority": 2,
             "tools": ["filesystem", "task", "defi_trade"]},
        ],
    }


# CT-1: the legacy per-stream seeder tags payload.cycle, this one tags
# payload.stream. During the documented cutover both timers can be armed.


def test_a_live_legacy_cycle_holds_the_streams_ceiling(board, manifest_path):
    """`scripts/seed_trading_cycle.py` writes `payload.cycle = "treasury-trading"`
    — the same string as the manifest stream id. A throttle that reads only
    `payload.stream` sees 0 live goals against a live legacy cycle and seeds a
    SECOND, overlapping cycle, each carrying `defi_trade`."""
    stream = S.load_manifest(manifest_path)[0]
    sid = stream["id"]
    legacy_objective = board.create_objective(
        user_id="rob", title="legacy trading objective", force=True)
    for i in range(3):
        board.create(user_id="rob", title=f"legacy leg {i}", priority=2,
                     parent_id=legacy_objective.id, force=True,
                     payload={"tools": ["defi_data", "defi_trade"], "cycle": sid})

    assert S.stream_live_goals(board, "rob", sid) == 3
    due, why = S.stream_is_due(board, "rob", stream, time.time())
    assert due is False, "a live legacy cycle must block a manifest re-seed"
    assert "ceiling" in why
    assert S.stream_last_seeded_at(board, "rob", sid) is not None


def test_a_done_legacy_cycle_does_not_hold_the_ceiling(board, manifest_path):
    """The legacy tag counts toward LIVE goals only while it IS live — a finished
    legacy cycle must not pin the stream shut forever."""
    stream = S.load_manifest(manifest_path)[0]
    sid = stream["id"]
    g = board.create(user_id="rob", title="legacy leg", priority=2, force=True,
                     payload={"tools": ["defi_trade"], "cycle": sid})
    _complete(board, g.id)
    assert S.stream_live_goals(board, "rob", sid) == 0


# CT-2: OBJECTIVE_GOAL_BUDGET is a LIFETIME tally (children_of drops only
# cancelled/dropped, so `done` children count forever and nothing sweeps them).


def test_a_standing_stream_reseeds_past_its_objective_goal_budget(board):
    """Four completed cycles of a 2-leg stream against `goal_budget: 4`.

    Pre-fix this raises "objective ... has spent its goal budget (4/4 live
    goals)" on cycle 3 and the stream is dead permanently — for the shipped
    3-leg/budget-12 trading stream that is ~16 hours on a fresh install. The
    stream's real throttle is `max_live_goals` + `cadence_hours`, which bound
    CONCURRENT work; the lifetime tally is a bounded-project rail.
    """
    stream = _two_leg_stream()
    oid, _ = S.ensure_objective(board, "rob", stream)
    for _cycle in range(4):
        goals = S.seed_stream(board, "rob", stream, oid)
        assert len(goals) == 2
        for g in goals:
            _complete(board, g.id)
    assert len(board.children_of("rob", oid)) == 8


def test_a_bounded_project_objective_still_enforces_its_goal_budget(board):
    """The exemption is keyed on `payload.stream_id` and must NOT widen to every
    objective — the lifetime tally is exactly what stops the planner opening
    "Round 9" on an objective that never finishes."""
    o = board.create_objective(user_id="rob", title="bounded project",
                               force=True, payload={"goal_budget": 2})
    board.create(user_id="rob", title="round one", parent_id=o.id, force=True)
    board.create(user_id="rob", title="round two", parent_id=o.id, force=True)
    with pytest.raises(ValueError, match="goal budget"):
        board.create(user_id="rob", title="round three", parent_id=o.id, force=True)


def test_seed_stream_is_all_or_nothing(board, monkeypatch):
    """A refusal landing MID-cycle must not leave leg 1 alone: it would hold a
    live slot against `max_live_goals` while the leg carrying the money verb
    never exists, so the stream reads busy and does nothing."""
    stream = _two_leg_stream()
    oid, _ = S.ensure_objective(board, "rob", stream)
    real_create = board.create
    calls = {"n": 0}

    def flaky_create(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("board refused the second leg")
        return real_create(*a, **kw)

    monkeypatch.setattr(board, "create", flaky_create)
    with pytest.raises(ValueError, match="second leg"):
        S.seed_stream(board, "rob", stream, oid)
    monkeypatch.undo()

    assert S.stream_live_goals(board, "rob", stream["id"]) == 0, \
        "the orphaned first leg still holds the stream's ceiling"


# CT-3: board.list ends `ORDER BY priority DESC, created_at LIMIT ?`, and a
# manifest stream sits BELOW the board default priority of 5 by design — so
# unrelated traffic evicts the stream's own LIVE rows first.


def _pad_board(board, n: int, *, user_id: str = "rob") -> None:
    """n ordinary priority-5 done rows — the traffic that crowded the window."""
    for i in range(n):
        g = board.create(user_id=user_id, title=f"unrelated work {i}", force=True)
        _complete(board, g.id)


def test_a_crowded_board_cannot_evict_the_streams_live_rows(board, manifest_path):
    stream = S.load_manifest(manifest_path)[0]
    sid = stream["id"]
    oid, _ = S.ensure_objective(board, "rob", stream)
    S.seed_stream(board, "rob", stream, oid)
    assert S.stream_live_goals(board, "rob", sid) == 2

    _pad_board(board, 1100)

    assert S.stream_live_goals(board, "rob", sid) == 2, \
        "the live cycle fell out of the scan window — the throttle inverted"
    assert S.stream_last_seeded_at(board, "rob", sid) is not None
    due, _why = S.stream_is_due(board, "rob", stream, time.time())
    assert due is False


def test_a_crowded_board_does_not_make_ensure_objective_mint_a_duplicate(board,
                                                                         manifest_path):
    """`ensure_objective` passes `force=True`, so the near-duplicate guard never
    catches the copy — a lost objective means a NEW one every hourly run."""
    stream = S.load_manifest(manifest_path)[0]
    first, _ = S.ensure_objective(board, "rob", stream)
    _pad_board(board, 1100)

    again, note = S.ensure_objective(board, "rob", stream)
    assert again == first, note
    assert len(board.objectives(user_id="rob", status="active")) == 1


# I4: adoption used to stamp stream_id ALONE, so success_criteria and
# goal_budget from the manifest were discarded permanently.


def test_adoption_applies_every_declared_objective_field(board, manifest_path):
    stream = S.load_manifest(manifest_path)[0]
    legacy = board.create_objective(
        user_id="rob", title=stream["objective"]["title"], force=True)

    oid, note = S.ensure_objective(board, "rob", stream)
    assert oid == legacy.id, note
    payload = board.get(oid).payload
    assert payload["stream_id"] == stream["id"]
    assert payload["success_criteria"] == stream["objective"]["success_criteria"]
    assert payload["goal_budget"] == stream["objective"]["goal_budget"]


def test_a_manifest_edit_reaches_an_existing_objective(board, manifest_path):
    """The manifest is the declarative source for its own streams, so editing a
    declared field must take effect on the next tick rather than being frozen at
    whatever the row held when it was created."""
    stream = S.load_manifest(manifest_path)[0]
    oid, _ = S.ensure_objective(board, "rob", stream)

    edited = dict(stream)
    edited["objective"] = dict(stream["objective"],
                               success_criteria="a different bar", goal_budget=99)
    again, note = S.ensure_objective(board, "rob", edited)
    assert again == oid, note
    payload = board.get(oid).payload
    assert payload["success_criteria"] == "a different bar"
    assert payload["goal_budget"] == 99


def test_re_applying_the_manifest_preserves_unrelated_payload_keys(board,
                                                                   manifest_path):
    stream = S.load_manifest(manifest_path)[0]
    oid, _ = S.ensure_objective(board, "rob", stream)
    board.merge_payload(oid, {"operator_note": "keep me"})
    S.ensure_objective(board, "rob", stream)
    assert board.get(oid).payload["operator_note"] == "keep me"


# I8: an hourly timer's failures otherwise reach nobody but journald.


def test_a_stream_failure_files_one_durable_owner_ask(board):
    S.record_stream_failure(board, "rob", "demo-stream", RuntimeError("boom"))
    S.record_stream_failure(board, "rob", "demo-stream", RuntimeError("boom again"))
    S.record_stream_failure(board, "rob", "other-stream", RuntimeError("different"))

    open_asks = board.asks(user_id="rob", status="open")
    by_stream = {(a.payload or {}).get("stream"): a for a in open_asks}
    assert set(by_stream) == {"demo-stream", "other-stream"}, \
        "each failing stream needs its own ask, not one fuzzily-merged one"
    assert by_stream["demo-stream"].payload["failures"] == 2
    assert "boom again" in by_stream["demo-stream"].payload["last_error"]


def test_a_clean_run_closes_a_standing_stream_failure_ask(board):
    S.record_stream_failure(board, "rob", "demo-stream", RuntimeError("boom"))
    S.clear_stream_failure(board, "rob", "demo-stream")
    assert board.asks(user_id="rob", status="open") == []


# --- 2026-08-29: standing missions are streams, not bounded projects ----------

def test_the_shipped_manifest_carries_the_two_standing_missions_without_money():
    """"Promote POLYROB … build-in-public" and "Build and ship real software
    artifacts" hit the 25-goal lifetime budget on prod (2026-08-28) and stalled the
    planner for a day: they are standing work modelled as bounded projects. As
    manifest streams they are uncapped by cadence, and — safety — grant no money
    verb (only the trading stream may)."""
    from core.tool_capabilities import ids_with
    MONEY_TOOLS = ids_with("money")
    got = {s["id"]: s for s in S.load_manifest(S.default_manifest_path())}
    assert "promote-build-in-public" in got and "ship-software" in got
    for sid in ("promote-build-in-public", "ship-software"):
        stream = got[sid]
        assert float(stream.get("cadence_hours") or 0) >= 12
        for g in stream["goals"]:
            assert not (set(g["tools"]) & set(MONEY_TOOLS)), (sid, g["title"])
    # exact-title adoption of the live objectives on prod
    assert got["promote-build-in-public"]["objective"]["title"] == \
        "Promote POLYROB and yourself in public — build-in-public"
    assert got["ship-software"]["objective"]["title"] == \
        "Build and ship real software artifacts"


def test_ship_software_stream_can_actually_ship():
    """Publishing & app-deployment evaluation 2026-09-05 (Wave 1): the goal body told
    the agent to run servers via the process tool while the grant omitted
    ``shell``/``process``, and the success criterion was satisfied by a curl against
    127.0.0.1 inside the container (91 green tests, 0 of 336 artifacts with a URL).
    The grant must carry the ship verbs and the criterion must demand a URL the
    owner can open — or a named reason it cannot be."""
    got = {s["id"]: s for s in S.load_manifest(S.default_manifest_path())}
    stream = got["ship-software"]
    for g in stream["goals"]:
        assert {"publish", "shell", "process"} <= set(g["tools"]), g["title"]
    crit = stream["objective"]["success_criteria"]
    assert "URL" in crit and "reason" in crit.lower()
    assert "HTTP-verified" not in crit
