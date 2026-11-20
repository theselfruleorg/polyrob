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


# --- proposal 125 S30 (D9): an unreadable store fails CLOSED ----------------


class _RaisingStore:
    """A store whose every read and write raises (a corrupt or locked db)."""

    def get_budget(self, session_id):
        raise RuntimeError("db unreadable")

    def consume_budget(self, *a, **k):
        raise RuntimeError("db unreadable")

    def put_budget(self, *a, **k):
        raise RuntimeError("db unreadable")

    def delete_budget(self, *a, **k):
        raise RuntimeError("db unreadable")


def test_try_consume_raises_when_store_unreadable():
    from agents.task.agent.core.self_wake import BudgetStoreUnreadable
    b = ReentryBudget(3, 0, clock=time.time, store=_RaisingStore())
    with pytest.raises(BudgetStoreUnreadable) as err:
        b.try_consume("s", "u")
    assert "db unreadable" in str(err.value.__cause__)   # the store's own error rides on


def test_record_counts_in_memory_when_store_unreadable():
    b = ReentryBudget(3, 0, clock=time.time, store=_RaisingStore())
    b.record("s", "u")                                     # never raises
    assert b._by_session["s"].count == 1


def test_allow_false_when_store_unreadable():
    b = ReentryBudget(3, 0, clock=time.time, store=_RaisingStore())
    assert b.allow("s") is False


def test_remaining_zero_when_store_unreadable():
    b = ReentryBudget(3, 0, clock=time.time, store=_RaisingStore())
    assert b.remaining("s") == 0


def test_no_store_path_unchanged():
    b = ReentryBudget(2, 0)
    assert b.allow("s") is True
    assert b.try_consume("s") is True
    assert b.remaining("s") == 1


@pytest.mark.asyncio
async def test_deliver_self_wake_skips_when_budget_unreadable(monkeypatch):
    import agents.task.agent.core.self_wake as sw
    import core.autonomy_control as ac
    import core.event_log as event_log
    events = []
    monkeypatch.setattr(event_log, "emit", lambda *a, **k: events.append(k.get("attrs")))
    monkeypatch.setattr(sw, "get_reentry_budget",
                        lambda: ReentryBudget(3, 0, clock=time.time, store=_RaisingStore()))
    monkeypatch.setattr(sw, "effective_self_wake_enabled", lambda *a, **k: True)
    monkeypatch.setattr(ac, "allows", lambda *a, **k: type("D", (), {"allowed": True})())
    from agents.task_agent_lite import TaskAgent
    agent = object.__new__(TaskAgent)
    assert await agent.deliver_self_wake("sess", "u-1", "wake up") is False
    assert {"outcome": "skipped", "reason": "budget_unreadable"} in events
