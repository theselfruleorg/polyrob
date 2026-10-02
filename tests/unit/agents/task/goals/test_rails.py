"""036 — standing work is configuration: rails, the grant store, the ONE predicate."""
import time

import pytest

from agents.task.goals import rails as R
from agents.task.goals.board import GoalBoard
from core import tool_grants as TG


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    for k in ("STREAM_SEEDING_PAUSE", "DATA_ROOT", "AUTONOMY_MODE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(R, "_owner_turn_live", lambda: None)
    return tmp_path


def _board(home):
    return GoalBoard(str(home / "goals.db"))


def _rail(b, name="watch", schedule="every 24h", legs=None, **kw):
    legs = legs or [{"title": "Watch the topic", "body": "look for news"}]
    return R.create_rail(b, "rob", title=name.title(), body="",
                         recurrence={"name": name, "schedule": schedule, "legs": legs, **kw})


# --- the predicate ---------------------------------------------------------------

def test_gated_ids_derive_from_the_capability_table():
    g = TG.gated_ids()
    assert {"defi_trade", "shell", "publish", "web_fetch"} <= g
    assert "filesystem" not in g and "task" not in g


def test_owner_seat_passes_everything():
    assert TG.assert_grantable(["defi_trade", "shell"], actor="owner_seat") == [
        "defi_trade", "shell"]


def test_agent_is_held_to_its_ceiling():
    assert TG.assert_grantable(["web_fetch", "filesystem"], actor="agent",
                               ceiling=["web_fetch"]) == ["web_fetch", "filesystem"]
    with pytest.raises(TG.GrantRefused) as e:
        TG.assert_grantable(["defi_trade"], actor="agent", ceiling=["web_fetch"])
    assert "defi_trade" in str(e.value)


def test_none_actor_cannot_carry_gated_tools():
    with pytest.raises(TG.GrantRefused):
        TG.assert_grantable(["shell"], actor="none")
    assert TG.assert_grantable(["filesystem"], actor="none") == ["filesystem"]


def test_unknown_actor_is_refused():
    with pytest.raises(TG.GrantRefused):
        TG.assert_grantable(["filesystem"], actor="planner")


def test_board_create_runs_the_predicate(home):
    b = _board(home)
    with pytest.raises(TG.GrantRefused):
        b.create(user_id="rob", title="x", payload={"tools": ["defi_trade"]},
                 actor="agent", tool_ceiling=["web_fetch"])
    assert b.list_recent(user_id="rob") == []
    g = b.create(user_id="rob", title="y", payload={"tools": ["defi_trade"]},
                 actor="owner_seat")
    assert g.payload["tools"] == ["defi_trade"]


def test_a_grant_read_never_creates_the_table(home):
    import sqlite3
    b = _board(home)
    assert TG.grants_for(b.db_path, user_id="rob", rail_id="r") == []
    con = sqlite3.connect(b.db_path)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    con.close()
    assert "rail_grants" not in names


def test_rail_actor_needs_a_grant_row(home):
    b = _board(home)
    with pytest.raises(TG.GrantRefused):
        TG.assert_grantable(["shell"], actor="rail", rail_id="r1", user_id="rob",
                            db_path=b.db_path, ceiling=["filesystem"])
    TG.grant_rail_tool(b.db_path, user_id="rob", rail_id="r1", tool_id="shell",
                       granted_by="rob@cli")
    assert TG.assert_grantable(["shell"], actor="rail", rail_id="r1", user_id="rob",
                               db_path=b.db_path, ceiling=["filesystem"]) == ["shell"]
    # tenant-scoped: another tenant's rail with the same id has no grant
    with pytest.raises(TG.GrantRefused):
        TG.assert_grantable(["shell"], actor="rail", rail_id="r1", user_id="eve",
                            db_path=b.db_path, ceiling=[])


def test_a_money_grant_needs_the_armed_regime(home, monkeypatch):
    b = _board(home)
    TG.grant_rail_tool(b.db_path, user_id="rob", rail_id="r1", tool_id="defi_trade",
                       granted_by="rob@cli")
    monkeypatch.setattr(TG, "money_armed", lambda: False)
    with pytest.raises(TG.GrantRefused) as e:
        TG.assert_grantable(["defi_trade"], actor="rail", rail_id="r1", user_id="rob",
                            db_path=b.db_path)
    assert "not armed" in str(e.value)
    monkeypatch.setattr(TG, "money_armed", lambda: True)
    assert TG.assert_grantable(["defi_trade"], actor="rail", rail_id="r1", user_id="rob",
                               db_path=b.db_path) == ["defi_trade"]


def test_grant_refuses_an_unknown_tool_and_revoke_is_immediate(home):
    b = _board(home)
    with pytest.raises(ValueError):
        TG.grant_rail_tool(b.db_path, user_id="rob", rail_id="r", tool_id="nope",
                           granted_by="rob")
    with pytest.raises(ValueError):
        TG.grant_rail_tool(b.db_path, user_id="rob", rail_id="r", tool_id="tool_manage",
                           granted_by="rob")
    TG.grant_rail_tool(b.db_path, user_id="rob", rail_id="r", tool_id="shell",
                       granted_by="rob")
    assert TG.revoke_rail_tool(b.db_path, user_id="rob", rail_id="r", tool_id="shell")
    assert TG.grants_for(b.db_path, user_id="rob", rail_id="r") == []


def test_pending_grants_are_not_read_at_seed_time(home):
    b = _board(home)
    TG.grant_rail_tool(b.db_path, user_id="rob", rail_id="r", tool_id="shell",
                       granted_by="import", pending=True)
    assert TG.grants_for(b.db_path, user_id="rob", rail_id="r") == []
    assert len(TG.grants_for(b.db_path, user_id="rob", rail_id="r",
                             include_pending=True)) == 1


# --- the definition --------------------------------------------------------------

def test_a_rail_leg_never_carries_tools(home):
    with pytest.raises(R.RailError):
        R.validate_recurrence({"schedule": "every 4h",
                               "legs": [{"title": "t", "body": "b", "tools": ["shell"]}]})
    with pytest.raises(R.RailError):
        R.validate_recurrence({"schedule": "every 4h", "tools": ["shell"],
                               "legs": [{"title": "t", "body": "b"}]})


def test_schedule_uses_the_one_parser_and_imports_cadence_hours():
    assert R.validate_recurrence({"cadence_hours": 4, "legs": [
        {"title": "t", "body": "b"}]})["schedule"] == "every 4h"
    with pytest.raises(R.RailError):
        R.validate_recurrence({"schedule": "whenever", "legs": [{"title": "t", "body": "b"}]})


def test_rail_is_an_objective_with_a_recurrence_and_is_uncapped(home):
    b = _board(home)
    row = _rail(b)
    assert row.kind == "objective" and R.is_rail(row)
    assert b.objective_budget(row) == 0
    with pytest.raises(R.RailError):
        _rail(b)  # same name


# --- seeding ----------------------------------------------------------------------

def test_seed_due_rails_seeds_once_then_waits(home):
    b = _board(home)
    row = _rail(b, legs=[{"title": "a", "body": "b"}, {"title": "c", "body": "d"}])
    out = R.seed_due_rails(b)
    assert out["seeded"] == 2
    legs = R.rail_goals(b, "rob", row.id)
    assert {g.payload["provenance"]["source"] for g in legs} == {"rail"}
    assert all(g.payload["tools"] == R.base_tools(R.recurrence_of(row)) for g in legs)
    second = [g for g in legs if g.title == "c"][0]
    assert b.dependencies(second.id)  # chained by default
    assert R.seed_due_rails(b)["seeded"] == 0  # ceiling / schedule hold


def test_independent_leg_is_not_chained(home):
    b = _board(home)
    row = _rail(b, legs=[{"title": "act", "body": "b"},
                         {"title": "report", "body": "d", "independent": True}])
    R.seed_due_rails(b)
    rep = [g for g in R.rail_goals(b, "rob", row.id) if g.title == "report"][0]
    assert b.dependencies(rep.id) == []


def test_pause_is_read_before_any_board_write(home, monkeypatch):
    from core import autonomy_control as ac
    b = _board(home)
    _rail(b)
    ac.pause(str(home), scopes=("streams",), via="test")
    calls = []
    monkeypatch.setattr(R, "_tenants_with_rails", lambda db: calls.append(db) or ["rob"])
    out = R.seed_due_rails(b)
    assert out["seeded"] == 0 and "paused" in out["skipped"] and calls == []
    ac.resume(str(home))


def test_a_live_owner_turn_defers_the_tick(home, monkeypatch):
    b = _board(home)
    _rail(b)
    monkeypatch.setattr(R, "_owner_turn_live", lambda: "a live owner turn (repl)")
    out = R.seed_due_rails(b)
    assert out["seeded"] == 0 and "deferred" in out["skipped"]


def test_off_rail_does_not_seed(home):
    b = _board(home)
    row = _rail(b)
    b.set_objective_status(row.id, "paused", user_id="rob")
    assert R.seed_due_rails(b)["seeded"] == 0


def test_schedule_window_reopens(home):
    b = _board(home)
    row = _rail(b, schedule="every 4h", max_live=5)
    t0 = time.time()
    assert R.seed_due_rails(b, now=t0)["seeded"] == 1
    assert R.seed_due_rails(b, now=t0 + 3600)["seeded"] == 0
    assert R.seed_due_rails(b, now=t0 + 4 * 3600)["seeded"] == 1
    assert len(R.rail_goals(b, "rob", row.id)) == 2


def test_grant_reaches_the_leg_and_money_stays_inert_unless_armed(home, monkeypatch):
    b = _board(home)
    row = _rail(b, name="trader")
    TG.grant_rail_tool(b.db_path, user_id="rob", rail_id=row.id, tool_id="defi_trade",
                       granted_by="rob@cli")
    TG.grant_rail_tool(b.db_path, user_id="rob", rail_id=row.id, tool_id="shell",
                       granted_by="rob@cli")
    monkeypatch.setattr(TG, "money_armed", lambda: False)
    R.seed_due_rails(b)
    [leg] = R.rail_goals(b, "rob", row.id)
    assert "shell" in leg.payload["tools"] and "defi_trade" not in leg.payload["tools"]
    b.cancel(leg.id, user_id="rob")
    b.merge_payload(row.id, {"recurrence": dict(R.recurrence_of(b.get(row.id)),
                                                last_seeded_at=0)})
    monkeypatch.setattr(TG, "money_armed", lambda: True)
    R.seed_due_rails(b, now=time.time() + 90000)
    newest = max(R.rail_goals(b, "rob", row.id), key=lambda g: g.created_at)
    assert "defi_trade" in newest.payload["tools"]


def test_a_suppressed_rail_does_not_seed(home):
    from core.goal_suppressions import SCOPE_STREAM, suppress
    b = _board(home)
    row = _rail(b)
    suppress(b.db_path, user_id="rob", scope=SCOPE_STREAM, value=row.id)
    assert R.seed_due_rails(b)["seeded"] == 0


def test_two_claims_cannot_both_win(home):
    b = _board(home)
    row = _rail(b)
    now = time.time()
    assert R._claim(b, row.id, now) is True
    assert R._claim(b, row.id, now + 1) is False
    assert R._claim(b, row.id, now + R.CLAIM_TTL_SEC + 5) is True


def test_undeclared_objective_is_listed(home):
    b = _board(home)
    o = b.create_objective(user_id="rob", title="Keep yourself capable")
    later = time.time() + R.UNDECLARED_AFTER_SEC + 10
    assert [x.id for x in R.undeclared(b, "rob", now=later)] == [o.id]


# --- import / export --------------------------------------------------------------

def test_import_turns_tools_into_pending_grants(home):
    b = _board(home)
    doc = {"streams": [{"id": "ship-software", "cadence_hours": 6,
                        "objective": {"title": "Ship software", "body": "b"},
                        "goals": [{"title": "build", "body": "do it",
                                   "tools": ["shell", "filesystem"]}]}]}
    out = R.import_manifest(b, "rob", doc)
    assert out["created"] == ["ship-software"] and out["pending_grants"] == [
        "ship-software:shell"]
    row, _ = R.find_rail(b, "rob", "ship-software")
    assert TG.grants_for(b.db_path, user_id="rob", rail_id=row.id) == []  # never applied
    exported = R.export_manifest(b, "rob")
    assert exported["streams"][0]["id"] == "ship-software"
    assert "tools" not in exported["streams"][0]["goals"][0]  # pending is not active
    assert R.import_manifest(b, "rob", doc)["kept"] == ["ship-software"]


# --- templates --------------------------------------------------------------------

def test_templates_render_and_never_carry_tools():
    from agents.task.goals.rail_templates import TEMPLATES, render
    assert len(TEMPLATES) == 5
    for t in TEMPLATES:
        assert "tools" not in t
        values = {s["name"]: (s.get("default") or "x") for s in t["slots"]}
        if t["key"] == "custom":
            values.update(title="t", body="b", schedule="every 24h")
        _title, _desc, rec = render(t["key"], values)
        R.validate_recurrence(rec)
    with pytest.raises(ValueError):
        render("topic-watch", {"tools": "defi_trade"})
    with pytest.raises(ValueError):
        render("topic-watch", {"interval": "5m"})


# --- the verb (one implementation, every seat) ------------------------------------

def _reply(b, *words):
    from surfaces.telegram.rail_ops import rail_reply
    return rail_reply("rob", "", list(words), board=b, seat="test")


def test_verb_new_list_show_off_on(home):
    b = _board(home)
    assert "No rails" in _reply(b)
    out = _reply(b, "new", "topic-watch", "topic=AI", "agents", "interval=12h")
    assert "ON" in out
    lst = _reply(b)
    assert "ON" in lst and "topic-watch" in lst
    assert "legs:" in _reply(b, "show", "topic-watch")
    assert "OFF" in _reply(b, "off", "topic-watch")
    assert "OFF" in _reply(b)
    assert "ON" in _reply(b, "on", "topic-watch")


def test_verb_grant_asks_first_then_writes(home):
    b = _board(home)
    _reply(b, "new", "ship-something")
    ask = _reply(b, "grant", "ship-something", "shell")
    assert "confirm" in ask
    row, _ = R.find_rail(b, "rob", "ship-something")
    assert TG.grants_for(b.db_path, user_id="rob", rail_id=row.id) == []
    done = _reply(b, "grant", "ship-something", "shell", "confirm")
    assert "Granted shell" in done
    [g] = TG.grants_for(b.db_path, user_id="rob", rail_id=row.id)
    assert g.granted_by == "rob@test"
    assert "Revoked" in _reply(b, "revoke", "ship-something", "shell")


def test_verb_drop_lets_live_legs_finish_by_default(home):
    b = _board(home)
    _reply(b, "new", "daily-digest")
    R.seed_due_rails(b)
    row, _ = R.find_rail(b, "rob", "daily-digest")
    out = _reply(b, "drop", "daily-digest")
    assert "will finish" in out and "--cancel-live" in out
    assert all(g.status != "cancelled" for g in R.rail_goals(b, "rob", row.id))
    assert b.get(row.id).status == "dropped"
    assert "No rails" in _reply(b) or "daily-digest" not in _reply(b)


def test_verb_drop_cancel_live(home):
    b = _board(home)
    _reply(b, "new", "daily-digest")
    R.seed_due_rails(b)
    row, _ = R.find_rail(b, "rob", "daily-digest")
    out = _reply(b, "drop", "daily-digest", "--cancel-live")
    assert "Cancelled 1" in out


def test_verb_edit_schedule_and_body(home):
    b = _board(home)
    _reply(b, "new", "weekly-review")
    assert "updated" in _reply(b, "edit", "weekly-review", "schedule", "every 7d")
    assert "updated" in _reply(b, "edit", "weekly-review", "body", "new words")
    row, _ = R.find_rail(b, "rob", "weekly-review")
    rec = R.recurrence_of(b.get(row.id))
    assert rec["schedule"] == "every 7d" and rec["legs"][0]["body"] == "new words"
    assert "Refused" in _reply(b, "edit", "weekly-review", "schedule", "never ever")


def test_verb_needs_an_owner():
    from surfaces.telegram.rail_ops import rail_reply
    assert "owner" in rail_reply(None, "", ["list"])


# --- proposals (036 §4.3) ---------------------------------------------------------

def test_proposal_is_consent_first_and_dismissal_is_latched(home):
    from agents.task.goals import rail_proposals as RP
    b = _board(home)
    RP.propose(b.db_path, user_id="rob", key="news", source="agent", title="News",
               recurrence={"schedule": "every 24h", "legs": [{"title": "n", "body": "b"}]},
               needs=["web_fetch"])
    assert R.list_rails(b, "rob") == []           # nothing created
    assert "PROPOSED" in _reply(b) and "needs grant: web_fetch" in _reply(b)
    assert "Dismissed" in _reply(b, "dismiss", "news")
    with pytest.raises(ValueError, match="never offered again"):
        RP.propose(b.db_path, user_id="rob", key="news", source="agent", title="News",
                   recurrence={"schedule": "every 24h",
                               "legs": [{"title": "n", "body": "b"}]})


def test_accepting_a_proposal_creates_the_rail_without_a_grant(home):
    from agents.task.goals import rail_proposals as RP
    b = _board(home)
    RP.propose(b.db_path, user_id="rob", key="trader", source="agent", title="Trade",
               recurrence={"schedule": "every 4h", "legs": [{"title": "t", "body": "b"}]},
               needs=["defi_trade"])
    out = _reply(b, "accept", "trader")
    assert "is ON" in out and "nothing is granted" in out
    row, _ = R.find_rail(b, "rob", "trader")
    assert row is not None
    assert TG.grants_for(b.db_path, user_id="rob", rail_id=row.id) == []


def test_proposals_are_capped(home):
    from agents.task.goals import rail_proposals as RP
    b = _board(home)
    for i in range(RP.MAX_PENDING):
        RP.propose(b.db_path, user_id="rob", key=f"k{i}", source="agent", title=f"t{i}",
                   recurrence={"schedule": "every 24h", "legs": [{"title": "t", "body": "b"}]})
    with pytest.raises(ValueError, match="already waiting"):
        RP.propose(b.db_path, user_id="rob", key="k9", source="agent", title="t9",
                   recurrence={"schedule": "every 24h", "legs": [{"title": "t", "body": "b"}]})


def test_a_proposal_can_never_carry_tools(home):
    from agents.task.goals import rail_proposals as RP
    b = _board(home)
    with pytest.raises(ValueError):
        RP.propose(b.db_path, user_id="rob", key="x", source="agent", title="x",
                   recurrence={"schedule": "every 24h",
                               "legs": [{"title": "t", "body": "b", "tools": ["shell"]}]})


def test_rail_propose_action_refuses_a_leaf_and_proposes_otherwise(home, monkeypatch):
    import asyncio
    import types
    from tools.goal_tools import GoalTool, RailProposeAction
    b = _board(home)
    tool = GoalTool.__new__(GoalTool)
    monkeypatch.setattr(GoalTool, "_resolve_board", lambda self: b, raising=False)
    monkeypatch.setattr(GoalTool, "_user", lambda self, ctx: "rob", raising=False)
    params = RailProposeAction(title="Weekly notes", body="summarize", schedule="every 7d")
    leaf = types.SimpleNamespace(is_sub_agent=True, role="leaf")
    r = asyncio.run(GoalTool.rail_propose.__wrapped__(tool, params, execution_context=leaf)
                    if hasattr(GoalTool.rail_propose, "__wrapped__")
                    else tool.rail_propose(params, execution_context=leaf))
    assert r.error and "leaf" in r.error
    ok = asyncio.run(tool.rail_propose(params, execution_context=types.SimpleNamespace()))
    assert ok.error is None and "Proposed rail" in ok.extracted_content


def test_a_money_rig_does_not_exceed_the_regime(home, monkeypatch):
    """A rail cannot pick up a money tool by naming a rig that holds one."""
    b = _board(home)
    row = _rail(b, name="mr", rig="money_rail")
    monkeypatch.setattr(TG, "money_armed", lambda: False)
    R.seed_due_rails(b)
    [leg] = R.rail_goals(b, "rob", row.id)
    assert "defi_trade" not in leg.payload["tools"] and "defi_data" in leg.payload["tools"]


@pytest.mark.parametrize("armed", [False, True])
def test_default_money_rig_never_supplies_an_ungranted_money_tool(home, monkeypatch, armed):
    from agents.task.goals.dispatcher import GoalDispatcher
    b = _board(home)
    monkeypatch.setenv("AUTONOMOUS_RIG_DEFAULT", "money_rail")
    monkeypatch.setattr(TG, "money_armed", lambda: armed)
    row = _rail(b)
    assert R.seed_due_rails(b)["seeded"] == 1
    [leg] = R.rail_goals(b, "rob", row.id)
    dispatch = object.__new__(GoalDispatcher)
    dispatch.board = b
    resolved = dispatch._resolve_tools(leg)
    assert "defi_data" in resolved
    assert "defi_trade" not in resolved
    assert resolved == leg.payload["tools"]


def test_default_money_rig_accepts_only_an_armed_money_grant(home, monkeypatch):
    b = _board(home)
    monkeypatch.setenv("AUTONOMOUS_RIG_DEFAULT", "money_rail")
    row = _rail(b)
    TG.grant_rail_tool(b.db_path, user_id="rob", rail_id=row.id, tool_id="defi_trade",
                       granted_by="rob@cli")
    monkeypatch.setattr(TG, "money_armed", lambda: False)
    assert "defi_trade" not in R.leg_tools(b, "rob", row)[0]
    monkeypatch.setattr(TG, "money_armed", lambda: True)
    assert "defi_trade" in R.leg_tools(b, "rob", row)[0]



def test_delayed_due_snapshot_cannot_seed_after_another_tick(home, monkeypatch):
    b = _board(home)
    row = _rail(b)
    claim = R._claim
    delayed = True
    nested = []
    def interleaved_claim(board, row_id, now):
        nonlocal delayed
        if delayed:
            delayed = False
            nested.append(R.seed_due_rails(board, now=now)["seeded"])
        return claim(board, row_id, now)
    monkeypatch.setattr(R, "_claim", interleaved_claim)
    assert R.seed_due_rails(b)["seeded"] == 0
    assert nested == [1]
    assert R.rail_live(b, "rob", row.id) == 1


def test_manual_seed_preserves_force_semantics(home):
    b = _board(home)
    row = _rail(b)
    now = time.time()
    assert len(R.seed_rail(b, "rob", row, now)) == 1
    assert len(R.seed_rail(b, "rob", row, now)) == 1


def test_automatic_cycle_waits_for_capacity_for_all_unsuppressed_legs(home):
    b = _board(home)
    row = _rail(b, max_live=3, legs=[{"title": "a", "body": "a"},
                                     {"title": "b", "body": "b"}])
    now = time.time()
    assert R.seed_due_rails(b, now=now)["seeded"] == 2
    # Schedule is due again, but only one of the two needed slots is free.
    assert R.seed_due_rails(b, now=now + 90000)["seeded"] == 0
    assert R.rail_live(b, "rob", row.id) == 2


def test_capacity_counts_only_unsuppressed_legs(home):
    from core.goal_suppressions import SCOPE_TITLE, suppress
    b = _board(home)
    row = _rail(b, max_live=1, legs=[{"title": "a", "body": "a"},
                                     {"title": "b", "body": "b"}])
    suppress(b.db_path, user_id="rob", scope=SCOPE_TITLE, value="a")
    assert R.seed_due_rails(b)["seeded"] == 1
    assert [g.title for g in R.rail_goals(b, "rob", row.id)] == ["b"]
