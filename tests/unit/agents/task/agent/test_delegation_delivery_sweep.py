"""T1.6 — restart-durable delegation delivery: completed-undelivered sweep.

Closes the gap: a background delegation whose child FINISHED
(``record_terminal`` persisted its result) but whose delivery to the
originating session never happened before the process died was orphaned
forever — the result sat in ``autonomy_state.db``, never surfaced.

``recover_interrupted_delegations`` runs a SECOND cold-start pass over rows
``status IN ('completed','error','timeout') AND delivered_at IS NULL`` and
SURFACES the already-computed result exactly once per cold start via the
self-wake rail. The existing first pass (still-``running`` rows ->
``interrupted``) is untouched; see ``test_autonomy_state_durable.py`` for its
unmodified coverage.

Review fix (2026-07-22, CRITICAL + IMPORTANT findings): ``delivered_at`` is no
longer stamped by this sweep (surfacing a wake only PARKS the result in the
target session's HITL queue — that is not "delivered"). The ONE stamp site is
now the drain path, :func:`agents.task.agent.autonomy_state.stamp_delivered_from_drain`
called from ``agent/core/user_ingress.py::_drain_user_messages`` the instant a
parked message is actually consumed into a turn. ``mark_delivered`` is a CAS
(``delivered_at IS NULL``) so a duplicate drain (racing cold starts /
at-least-once redelivery) stamps at most once. These tests cover the store
primitives, the sweep's surface-only behavior, and the drain-path stamp
(exercised directly against ``stamp_delivered_from_drain``, which is the same
function the live async-delegation path and this sweep's self-wake path both
funnel through).
"""
import asyncio

import pytest

from agents.task.agent import autonomy_state
from agents.task.agent.autonomy_state import (
    AutonomyStateStore,
    recover_interrupted_delegations,
    stamp_delivered_from_drain,
)


@pytest.fixture()
def store(tmp_path):
    return AutonomyStateStore(str(tmp_path / "autonomy_state.db"))


class _Agent:
    """Fake task_agent capturing deliver_self_wake calls (always succeeds)."""

    def __init__(self):
        self.wakes = []

    async def deliver_self_wake(self, session_id, user_id, text, metadata=None):
        self.wakes.append((session_id, user_id, text, metadata))
        return True


class _RaisingAgent:
    async def deliver_self_wake(self, *a, **kw):
        raise RuntimeError("boom")


class _DisabledAgent:
    """Mirrors deliver_self_wake's real contract: returns False (no raise) when
    self-wake is disabled/budget-exhausted/session non-resident."""

    def __init__(self):
        self.calls = 0

    async def deliver_self_wake(self, *a, **kw):
        self.calls += 1
        return False


# --- store primitives ---------------------------------------------------------


def test_list_completed_undelivered_scoping(store):
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0001",
                            goal="g", profile="p", parent_agent_id=None, dispatched_at=1.0)
    store.record_terminal("s1", "deleg_0001", status="completed",
                          completed_at=2.0, result_text="the result")
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0002",
                            goal="g", profile="p", parent_agent_id=None, dispatched_at=1.0)
    store.record_terminal("s1", "deleg_0002", status="cancelled", completed_at=2.0)
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0003",
                            goal="g", profile="p", parent_agent_id=None, dispatched_at=1.0)
    # still running -> excluded (that's pass 1's job, not this query's)
    rows = store.list_completed_undelivered()
    ids = {r["delegation_id"] for r in rows}
    assert ids == {"deleg_0001"}  # cancelled + running excluded


def test_mark_delivered_removes_row_from_undelivered_list(store):
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0001",
                            goal="g", profile="p", parent_agent_id=None, dispatched_at=1.0)
    store.record_terminal("s1", "deleg_0001", status="completed",
                          completed_at=2.0, result_text="r")
    assert len(store.list_completed_undelivered()) == 1
    n = store.mark_delivered("s1", "deleg_0001", 3.0)
    assert n == 1
    assert store.list_completed_undelivered() == []
    assert store.get("s1", "deleg_0001")["delivered_at"] == 3.0


def test_mark_delivered_is_cas_double_call_stamps_once(store):
    """IMPORTANT finding fix: mark_delivered must be a CAS (delivered_at IS
    NULL), not a bare UPDATE — two racing cold starts / a duplicate drain must
    stamp the row (and any delivered_at-gated event) exactly once."""
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0001",
                            goal="g", profile="p", parent_agent_id=None, dispatched_at=1.0)
    store.record_terminal("s1", "deleg_0001", status="completed",
                          completed_at=2.0, result_text="r")

    n1 = store.mark_delivered("s1", "deleg_0001", 3.0)
    n2 = store.mark_delivered("s1", "deleg_0001", 5.0)  # a later, racing stamp

    assert n1 == 1  # first caller wins
    assert n2 == 0  # CAS no-op — already stamped
    assert store.get("s1", "deleg_0001")["delivered_at"] == 3.0  # unchanged by n2


def test_delivered_at_column_migrated_onto_pre_existing_db(tmp_path):
    """A DB created by an older build (no delivered_at column) must not crash
    on open — the guarded ALTER TABLE backfills it."""
    import core.sqlite_util as sqlu

    db_path = str(tmp_path / "legacy_autonomy_state.db")
    sqlu.execute_retry(
        db_path,
        """CREATE TABLE delegations (
               session_id TEXT NOT NULL,
               delegation_id TEXT NOT NULL,
               user_id TEXT NOT NULL DEFAULT '',
               goal TEXT, profile TEXT, parent_agent_id TEXT,
               status TEXT NOT NULL DEFAULT 'running',
               dispatched_at REAL, completed_at REAL, result_text TEXT,
               PRIMARY KEY (session_id, delegation_id)
           )""",
    )
    legacy_store = AutonomyStateStore(db_path)  # must not raise
    legacy_store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0001",
                                   goal="g", profile="p", parent_agent_id=None,
                                   dispatched_at=1.0)
    legacy_store.record_terminal("s1", "deleg_0001", status="completed",
                                 completed_at=2.0, result_text="r")
    assert len(legacy_store.list_completed_undelivered()) == 1


# --- sweep: surfacing (CRITICAL finding fix: the sweep dispatches, it does
# NOT stamp delivered_at — that happens only on drain) --------------------------


def test_sweep_surfaces_completed_undelivered_with_honest_framing_but_does_not_stamp(store):
    """Review fix: dispatching the self-wake only PARKS the result in the
    target session's HITL queue. It is not "delivered" — delivered_at stays
    NULL until a later drain actually consumes the message into a turn (see
    test_stamp_delivered_from_drain_* below for that half of the contract)."""
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0001",
                            goal="research thing", profile="executor",
                            parent_agent_id="a1", dispatched_at=1.0)
    store.record_terminal("s1", "deleg_0001", status="completed",
                          completed_at=2.0, result_text="the child's actual output")

    agent = _Agent()
    n = asyncio.run(recover_interrupted_delegations(agent, store.db_path))

    assert n == 1  # 1 row SURFACED this cold start
    assert len(agent.wakes) == 1
    sid, uid, text, meta = agent.wakes[0]
    assert sid == "s1" and uid == "u1"
    assert "deleg_0001" in text
    assert "the child's actual output" in text
    assert "recovered after restart" in text.lower()
    assert meta["delegation_id"] == "deleg_0001"

    row = store.get("s1", "deleg_0001")
    assert row["delivered_at"] is None  # NOT stamped by the sweep itself


def test_sweep_resurfaces_every_cold_start_until_actually_drained(store):
    """IMPORTANT finding fix, honest retry contract: a surfaced-but-never-
    drained wake (e.g. the kicked run never reaches drain, or the session
    stays idle) must NOT be silently forgotten — every subsequent cold start
    re-surfaces it, since delivered_at is only ever stamped on drain."""
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0001",
                            goal="g", profile="p", parent_agent_id=None, dispatched_at=1.0)
    store.record_terminal("s1", "deleg_0001", status="completed",
                          completed_at=2.0, result_text="r")

    agent = _Agent()
    n1 = asyncio.run(recover_interrupted_delegations(agent, store.db_path))
    n2 = asyncio.run(recover_interrupted_delegations(agent, store.db_path))

    assert n1 == 1
    assert n2 == 1  # re-surfaced: nothing ever drained it
    assert len(agent.wakes) == 2
    assert store.get("s1", "deleg_0001")["delivered_at"] is None


def test_sweep_surfaces_nothing_after_a_drained_delivery(store):
    """Repeated sweep after a drained delivery surfaces nothing (T1.6 review
    fix requirement). Simulates the drain-path stamp directly via
    mark_delivered (the same primitive stamp_delivered_from_drain calls) —
    once a row is genuinely drained, list_completed_undelivered excludes it
    and the sweep has nothing left to surface."""
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0001",
                            goal="g", profile="p", parent_agent_id=None, dispatched_at=1.0)
    store.record_terminal("s1", "deleg_0001", status="completed",
                          completed_at=2.0, result_text="r")

    agent = _Agent()
    n1 = asyncio.run(recover_interrupted_delegations(agent, store.db_path))
    assert n1 == 1
    assert len(agent.wakes) == 1

    # Simulate the kicked run's drain consuming the parked message into a turn.
    assert store.mark_delivered("s1", "deleg_0001", 9.0) == 1

    n2 = asyncio.run(recover_interrupted_delegations(agent, store.db_path))

    assert n2 == 0
    assert len(agent.wakes) == 1  # never re-surfaced once genuinely delivered


def test_already_delivered_row_never_redelivered(store):
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0001",
                            goal="g", profile="p", parent_agent_id=None, dispatched_at=1.0)
    store.record_terminal("s1", "deleg_0001", status="completed",
                          completed_at=2.0, result_text="r")
    store.mark_delivered("s1", "deleg_0001", 3.0)

    agent = _Agent()
    n = asyncio.run(recover_interrupted_delegations(agent, store.db_path))

    assert n == 0
    assert agent.wakes == []


def test_error_and_timeout_statuses_are_also_delivered_with_honest_verb(store):
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0001",
                            goal="g", profile="p", parent_agent_id=None, dispatched_at=1.0)
    store.record_terminal("s1", "deleg_0001", status="error",
                          completed_at=2.0, result_text="boom")
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0002",
                            goal="g2", profile="p", parent_agent_id=None, dispatched_at=1.0)
    store.record_terminal("s1", "deleg_0002", status="timeout",
                          completed_at=2.0, result_text="took too long")

    agent = _Agent()
    n = asyncio.run(recover_interrupted_delegations(agent, store.db_path))

    assert n == 2
    texts = {w[2] for w in agent.wakes}
    assert any("failed" in t and "boom" in t for t in texts)
    assert any("timed out" in t and "took too long" in t for t in texts)


def test_cancelled_row_is_never_delivered_or_stamped(store):
    """A cancelled delegation never produced a real result — it must not be
    resurrected as a fake completion after restart."""
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0001",
                            goal="g", profile="p", parent_agent_id=None, dispatched_at=1.0)
    store.record_terminal("s1", "deleg_0001", status="cancelled", completed_at=2.0)

    agent = _Agent()
    n = asyncio.run(recover_interrupted_delegations(agent, store.db_path))

    assert n == 0
    assert agent.wakes == []
    assert store.get("s1", "deleg_0001")["delivered_at"] is None


# --- sweep: fail-open honesty -----------------------------------------------


def test_sweep_delivery_failure_leaves_row_unstamped_and_does_not_raise(store):
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0001",
                            goal="g", profile="p", parent_agent_id=None, dispatched_at=1.0)
    store.record_terminal("s1", "deleg_0001", status="completed",
                          completed_at=2.0, result_text="r")

    n = asyncio.run(recover_interrupted_delegations(_RaisingAgent(), store.db_path))

    assert n == 0  # sweep must not raise
    assert store.get("s1", "deleg_0001")["delivered_at"] is None  # recoverable next time


def test_sweep_never_stamps_when_deliver_self_wake_returns_false(store):
    """deliver_self_wake's real contract returns False (no exception) when
    self-wake is disabled/budget-exhausted/session non-resident — that must be
    treated as non-delivery, not silently marked delivered."""
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0001",
                            goal="g", profile="p", parent_agent_id=None, dispatched_at=1.0)
    store.record_terminal("s1", "deleg_0001", status="completed",
                          completed_at=2.0, result_text="r")

    agent = _DisabledAgent()
    n = asyncio.run(recover_interrupted_delegations(agent, store.db_path))

    assert n == 0
    assert agent.calls == 1
    assert store.get("s1", "deleg_0001")["delivered_at"] is None


def test_sweep_noop_when_task_agent_has_no_deliver_self_wake(store):
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0001",
                            goal="g", profile="p", parent_agent_id=None, dispatched_at=1.0)
    store.record_terminal("s1", "deleg_0001", status="completed",
                          completed_at=2.0, result_text="r")

    n = asyncio.run(recover_interrupted_delegations(object(), store.db_path))

    assert n == 0
    assert store.get("s1", "deleg_0001")["delivered_at"] is None


# --- existing (still-running -> interrupted) behavior stays untouched ----------


def test_running_row_still_recovered_as_interrupted_alongside_completed_pass(store):
    """Both passes run in one sweep call: a still-running row goes to
    'interrupted' exactly as before, independent of the completed-undelivered
    pass touching a different row."""
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0001",
                            goal="still going", profile="p", parent_agent_id=None,
                            dispatched_at=1.0)
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0002",
                            goal="already done", profile="p", parent_agent_id=None,
                            dispatched_at=1.0)
    store.record_terminal("s1", "deleg_0002", status="completed",
                          completed_at=2.0, result_text="r")

    agent = _Agent()
    n = asyncio.run(recover_interrupted_delegations(agent, store.db_path))

    assert n == 2
    assert store.get("s1", "deleg_0001")["status"] == "interrupted"
    assert store.get("s1", "deleg_0002")["status"] == "completed"
    # Review fix: surfacing (dispatching the wake) no longer stamps
    # delivered_at — only a genuine drain does (see
    # test_stamp_delivered_from_drain_* below).
    assert store.get("s1", "deleg_0002")["delivered_at"] is None
    assert len(agent.wakes) == 2


# --- drain-path stamp: stamp_delivered_from_drain (CRITICAL finding fix) -------
#
# This is the ONE place delivered_at is stamped, called from
# agent/core/user_ingress.py::_drain_user_messages. Both producers of a
# redeliverable result funnel through it: the live async-delegation path
# (kind=DELEGATION_RESULT_KIND) and this module's completed-undelivered sweep,
# via TaskAgent.deliver_self_wake (kind=SELF_WAKE_KIND). Exercised directly
# here since it's the same function both call sites use.

from core.security.forged_turns import DELEGATION_RESULT_KIND, SELF_WAKE_KIND  # noqa: E402


def test_stamp_delivered_from_drain_live_path_delegation_result_kind(store, monkeypatch):
    monkeypatch.setattr(autonomy_state, "get_autonomy_state_store", lambda: store)
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0001",
                            goal="g", profile="p", parent_agent_id=None, dispatched_at=1.0)
    store.record_terminal("s1", "deleg_0001", status="completed",
                          completed_at=2.0, result_text="r")

    stamp_delivered_from_drain(
        "s1", "u1", DELEGATION_RESULT_KIND,
        {"source": "async_delegation", "delegation_id": "deleg_0001", "status": "completed"},
    )

    assert store.get("s1", "deleg_0001")["delivered_at"] is not None


def test_stamp_delivered_from_drain_sweep_path_self_wake_kind(store, monkeypatch):
    monkeypatch.setattr(autonomy_state, "get_autonomy_state_store", lambda: store)
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0001",
                            goal="g", profile="p", parent_agent_id=None, dispatched_at=1.0)
    store.record_terminal("s1", "deleg_0001", status="completed",
                          completed_at=2.0, result_text="r")

    stamp_delivered_from_drain(
        "s1", "u1", SELF_WAKE_KIND,
        {"delegation_id": "deleg_0001", "status": "completed",
         "recovery": "completed_undelivered_delegation"},
    )

    assert store.get("s1", "deleg_0001")["delivered_at"] is not None


def test_stamp_delivered_from_drain_ignores_non_forged_kind_even_with_delegation_id(store, monkeypatch):
    """Security scoping: metadata on an ordinary 'comment'/'guidance' message
    is caller-supplied over the public HTTP API (request.metadata) — a
    delegation_id field-name collision there must never be able to force a
    stamp."""
    monkeypatch.setattr(autonomy_state, "get_autonomy_state_store", lambda: store)
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0001",
                            goal="g", profile="p", parent_agent_id=None, dispatched_at=1.0)
    store.record_terminal("s1", "deleg_0001", status="completed",
                          completed_at=2.0, result_text="r")

    stamp_delivered_from_drain("s1", "u1", "comment", {"delegation_id": "deleg_0001"})

    assert store.get("s1", "deleg_0001")["delivered_at"] is None


def test_stamp_delivered_from_drain_noop_when_no_delegation_id(store, monkeypatch):
    monkeypatch.setattr(autonomy_state, "get_autonomy_state_store", lambda: store)
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0001",
                            goal="g", profile="p", parent_agent_id=None, dispatched_at=1.0)
    store.record_terminal("s1", "deleg_0001", status="completed",
                          completed_at=2.0, result_text="r")

    stamp_delivered_from_drain("s1", "u1", DELEGATION_RESULT_KIND, {"source": "async_delegation"})
    stamp_delivered_from_drain("s1", "u1", DELEGATION_RESULT_KIND, None)
    stamp_delivered_from_drain("s1", "u1", DELEGATION_RESULT_KIND, {})

    assert store.get("s1", "deleg_0001")["delivered_at"] is None


def test_stamp_delivered_from_drain_noop_when_store_unavailable(monkeypatch):
    """Durability off / store unreachable: fail-open, never raises."""
    monkeypatch.setattr(autonomy_state, "get_autonomy_state_store", lambda: None)
    stamp_delivered_from_drain(
        "s1", "u1", DELEGATION_RESULT_KIND,
        {"delegation_id": "deleg_0001"},
    )  # must not raise


def test_stamp_delivered_from_drain_is_cas_safe_for_duplicate_drain(store, monkeypatch):
    """A duplicate drain (both copies of an at-least-once redelivered result,
    or a racing cold-start re-wake) stamps the row at most once."""
    monkeypatch.setattr(autonomy_state, "get_autonomy_state_store", lambda: store)
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0001",
                            goal="g", profile="p", parent_agent_id=None, dispatched_at=1.0)
    store.record_terminal("s1", "deleg_0001", status="completed",
                          completed_at=2.0, result_text="r")

    metadata = {"delegation_id": "deleg_0001", "status": "completed"}
    stamp_delivered_from_drain("s1", "u1", DELEGATION_RESULT_KIND, metadata)
    first = store.get("s1", "deleg_0001")["delivered_at"]
    stamp_delivered_from_drain("s1", "u1", DELEGATION_RESULT_KIND, metadata)
    second = store.get("s1", "deleg_0001")["delivered_at"]

    assert first is not None
    assert second == first  # unchanged by the duplicate call


def test_stamp_delivered_from_drain_rejects_spoofed_kind_on_still_running_row(store, monkeypatch):
    """CRITICAL finding fix: kind + metadata are both caller-controllable over
    the public HTTP API (POST /sessions/{sid}/messages has no kind allowlist,
    request.metadata is free-form). An owner who learns a delegation_id from
    their own still-RUNNING dispatch must not be able to pre-stamp it by
    forging kind="delegation_result" — the row must stay eligible for honest
    at-least-once delivery once it actually completes."""
    monkeypatch.setattr(autonomy_state, "get_autonomy_state_store", lambda: store)
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0001",
                            goal="still running", profile="p", parent_agent_id=None,
                            dispatched_at=1.0)
    # No record_terminal — the row is still 'running'.

    stamp_delivered_from_drain(
        "s1", "u1", DELEGATION_RESULT_KIND,
        {"delegation_id": "deleg_0001", "status": "completed"},
    )

    assert store.get("s1", "deleg_0001")["delivered_at"] is None

    # Once it genuinely completes, list_completed_undelivered still surfaces
    # it — the spoofed call above did not orphan the row.
    store.record_terminal("s1", "deleg_0001", status="completed",
                          completed_at=2.0, result_text="the real result")
    ids = {r["delegation_id"] for r in store.list_completed_undelivered()}
    assert "deleg_0001" in ids


def test_stamp_delivered_from_drain_stamps_once_row_is_genuinely_terminal(store, monkeypatch):
    """Legit-path regression guard: once record_terminal has actually run, the
    same call (same kind, same metadata) that was rejected pre-terminal now
    stamps delivered_at — the terminal-status gate does not disturb the
    legitimate drain path."""
    monkeypatch.setattr(autonomy_state, "get_autonomy_state_store", lambda: store)
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0001",
                            goal="g", profile="p", parent_agent_id=None, dispatched_at=1.0)
    store.record_terminal("s1", "deleg_0001", status="completed",
                          completed_at=2.0, result_text="r")

    stamp_delivered_from_drain(
        "s1", "u1", DELEGATION_RESULT_KIND,
        {"delegation_id": "deleg_0001", "status": "completed"},
    )

    assert store.get("s1", "deleg_0001")["delivered_at"] is not None
