"""The treasury-trading cycle seeder tops the board up to ONE live cycle.

The property that matters operationally: running it on a timer must never pile
up duplicate money-granting goals while a cycle is still in flight.
"""
import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "seed_trading_cycle", _ROOT / "scripts" / "seed_trading_cycle.py")
seeder = importlib.util.module_from_spec(_spec)
sys.modules["seed_trading_cycle"] = seeder
_spec.loader.exec_module(seeder)


@pytest.fixture
def board(tmp_path):
    from agents.task.goals.board import GoalBoard
    return GoalBoard(str(tmp_path / "goals.db"))


class TestCycleSpecs:
    def test_only_the_trade_leg_carries_the_money_verb(self):
        by_title = {s["title"]: s["payload"]["tools"] for s in seeder.cycle_specs()}
        granted = [t for t, tools in by_title.items() if "defi_trade" in tools]
        assert len(granted) == 1, "exactly one leg may hold the money grant"
        assert "manage open positions" in granted[0]

    def test_every_leg_is_tagged_so_the_top_up_can_count_it(self):
        for spec in seeder.cycle_specs():
            assert spec["payload"]["cycle"] == seeder.CYCLE_TAG

    def test_no_leg_pins_a_provider(self):
        """A frozen pin outlives the account that served it."""
        for spec in seeder.cycle_specs():
            assert "provider" not in spec["payload"]
            assert "model" not in spec["payload"]

    def test_the_trade_leg_has_step_headroom_to_actually_execute(self):
        trade = [s for s in seeder.cycle_specs() if "defi_trade" in s["payload"]["tools"]][0]
        # dry-run, approve, swap, revoke, ledger, plus exits — a low cap marks a
        # goal done while it is still drafting.
        assert trade["payload"]["max_steps"] >= 30


class TestTopUp:
    def test_counts_nothing_on_an_empty_board(self, board):
        assert seeder.live_cycle_goals(board, "rob") == 0

    def test_counts_live_cycle_goals(self, board):
        for spec in seeder.cycle_specs():
            board.create(user_id="rob", title=spec["title"], body=spec["body"],
                         priority=spec["priority"], payload=spec["payload"])
        assert seeder.live_cycle_goals(board, "rob") == 3

    def test_ignores_a_finished_cycle(self, board):
        spec = seeder.cycle_specs()[0]
        goal = board.create(user_id="rob", title=spec["title"], body=spec["body"],
                            priority=spec["priority"], payload=spec["payload"])
        board.mark_done(goal.id, result="done") if hasattr(board, "mark_done") else None
        import sqlite3
        con = sqlite3.connect(board.db_path)
        con.execute("UPDATE goals SET status='done' WHERE id=?", (goal.id,))
        con.commit(); con.close()
        assert seeder.live_cycle_goals(board, "rob") == 0

    def test_ignores_other_work_on_the_board(self, board):
        board.create(user_id="rob", title="Something else entirely",
                     body="unrelated work", priority=3, payload={"tools": ["task"]})
        assert seeder.live_cycle_goals(board, "rob") == 0

    def test_main_seeds_nothing_while_a_cycle_is_live(self, board, capsys):
        for spec in seeder.cycle_specs():
            board.create(user_id="rob", title=spec["title"], body=spec["body"],
                         priority=spec["priority"], payload=spec["payload"])
        rc = seeder.main(["--db", board.db_path, "--user-id", "rob"])
        assert rc == 0
        assert "seeding nothing" in capsys.readouterr().out
        assert seeder.live_cycle_goals(board, "rob") == 3


class TestObjective:
    def test_is_created_once_and_then_recognised(self, board):
        first = seeder.ensure_objective(board, "rob")
        assert "created" in first
        second = seeder.ensure_objective(board, "rob")
        assert "already active" in second


class TestRecurringSeedIsNotBlockedByItsOwnHistory:
    """The cycle reuses its titles every pass, and the board's near-duplicate
    guard also matches COMPLETED rows -- so without force the second cycle
    scores 1.00 against the first and the loop dies after one pass."""

    def test_a_second_cycle_seeds_after_the_first_completed(self, board):
        import sqlite3
        seeder.main(["--db", board.db_path, "--user-id", "rob"])
        first = {g.id for g in board.list(user_id="rob", limit=500)}
        assert len(first) == 3

        con = sqlite3.connect(board.db_path)
        con.execute("UPDATE goals SET status='done'")
        con.commit(); con.close()

        rc = seeder.main(["--db", board.db_path, "--user-id", "rob"])
        assert rc == 0
        second = {g.id for g in board.list(user_id="rob", limit=500)} - first
        assert len(second) == 3, "the next cycle must seed despite identical titles"


class TestCycleIsOrdered:
    """scan -> trade -> publish must be a chain, not three racing goals.

    Live prod, first degen cycle: GOAL_MAX_CONCURRENT=2 dispatched the scan and
    the trade leg together, so the trade leg read an EMPTY watchlist and
    correctly did nothing -- meaning the cycle could never trade. Publish then
    reported on a ledger the trade leg had not written.
    """

    def test_seeded_legs_are_chained(self, board):
        seeder.main(["--db", board.db_path, "--user-id", "rob"])
        goals = {g.title: g for g in board.list(user_id="rob", limit=500)}
        scan = next(g for t, g in goals.items() if "watchlist" in t)
        trade = next(g for t, g in goals.items() if "manage open positions" in t)
        publish = next(g for t, g in goals.items() if "publish" in t.lower())

        assert scan.status == "ready", "the scan leg starts the chain"
        assert trade.status == "waiting", "trade must wait for the watchlist"
        assert publish.status == "waiting", "publish must wait for the trade leg"

    def test_the_chain_runs_in_order_as_each_leg_lands(self, board):
        seeder.main(["--db", board.db_path, "--user-id", "rob"])
        goals = {g.title: g for g in board.list(user_id="rob", limit=500)}
        scan = next(g for t, g in goals.items() if "watchlist" in t)
        trade = next(g for t, g in goals.items() if "manage open positions" in t)

        # record_success only fires the dependent sweep from 'running'.
        board.update_status(scan.id, "running")
        board.record_success(scan.id, result="watchlist written")
        refreshed = {g.id: g for g in board.list(user_id="rob", limit=500)}
        assert refreshed[trade.id].status == "ready", \
            "the trade leg must release once the watchlist exists"
