"""098 AG-11: with a store, the DB counter is the reentry budget's source."""
import time

import pytest

from agents.task.agent.autonomy_state import STALE_BUDGET_SEC, AutonomyStateStore
from agents.task.agent.core.self_wake import ReentryBudget


@pytest.fixture
def store(tmp_path):
    return AutonomyStateStore(str(tmp_path / "autonomy_state.db"))


def _budget(store, clock=time.time):
    return ReentryBudget(3, 0, clock=clock, store=store)


def test_two_instances_share_cap(store):
    a, b = _budget(store), _budget(store)
    assert a.try_consume("s", "u1") and b.try_consume("s") and a.try_consume("s")
    assert b.try_consume("s") is False and a.try_consume("s") is False
    assert store.get_budget("s")["count"] == 3


def test_reset_in_one_instance_visible_in_other(store):
    a, b = _budget(store), _budget(store)
    for _ in range(3):
        assert a.try_consume("s")
    assert b.allow("s") is False
    a.reset("s")
    assert b.allow("s") is True and b.try_consume("s") is True


def test_stale_row_allows_wake_without_restart(store):
    t = {"now": 1_000_000.0}
    a = _budget(store, clock=lambda: t["now"])
    for _ in range(3):
        assert a.try_consume("s")
    assert a.try_consume("s") is False
    t["now"] += STALE_BUDGET_SEC + 1
    assert a.try_consume("s") is True
    assert store.get_budget("s")["count"] == 1


def test_backoff_is_shared(store):
    t = {"now": 1_000_000.0}
    a = ReentryBudget(5, 60, clock=lambda: t["now"], store=store)
    b = ReentryBudget(5, 60, clock=lambda: t["now"], store=store)
    assert a.try_consume("s")
    assert b.try_consume("s") is False
    t["now"] += 61
    assert b.try_consume("s") is True


def test_user_id_is_persisted(store):
    _budget(store).try_consume("s", "alice")
    from core.sqlite_util import execute_retry
    row = execute_retry(store.db_path, "SELECT user_id FROM reentry_budget WHERE session_id='s'",
                        (), fetch="one")
    assert row["user_id"] == "alice"
