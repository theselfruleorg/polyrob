"""044 T20: a goal carrying ``payload.group`` runs AS the room session.

Two things have to be true at once or the run is a privacy hole:

1. ``create_session`` receives the ROOM's own ``session_source`` +
   ``chat_session_key`` — that is what stamps ``_public_session`` and points
   every reply at the room rather than at the owner's DM.
2. The toolset is ``room_tool_ids()`` and ``payload.tools`` is IGNORED. A room
   session is PUBLIC; a goal payload asking for ``defi_trade`` must not be able
   to widen it, and must say so in the log rather than silently.

The whole chain is exercised (dispatcher -> ``run_task_to_outcome`` ->
``create_session``), because the forwarding hop is exactly where the binding
used to be dropped.
"""
import asyncio
import logging
import time
from types import SimpleNamespace

from agents.task.goals.board import Goal, STATUS_READY
from agents.task.goals.dispatcher import GoalDispatcher
from core.surfaces.group_ledger import GroupLedger, LedgerRow
from core.surfaces.room_policy import room_tool_ids

#: ⚠️ Ledger rows prune by WALL CLOCK, so every fixture ts is relative to now.
NOW = time.time()


class _FakeBoard:
    def __init__(self):
        self.successes, self.failures, self.results = [], [], []
        self._status = "running"

    def record_success(self, gid, session_id=None, result=None):
        self.successes.append(gid)
        self.results.append(result)
        self._status = "done"

    def record_failure(self, gid, error=None, session_id=None):
        self.failures.append((gid, error))
        self._status = STATUS_READY
        return Goal(id=gid, user_id="rob", title="t", status=STATUS_READY)

    def get(self, gid):
        return Goal(id=gid, user_id="rob", title="t", status=self._status)

    def set_outcome(self, gid, outcome):
        return True

    def create_ask(self, **kw):
        return None


class _Container:
    def __init__(self, tmp_path):
        self._svc = {"group_ledger": GroupLedger(str(tmp_path / "surfaces.db"))}
        self.config = SimpleNamespace(data_dir=str(tmp_path))

    def get_service(self, n):
        return self._svc.get(n)


class _TaskAgent:
    """Records what ``create_session`` was handed; runs nothing."""

    def __init__(self, container, status="no active session found"):
        self.container = container
        self.calls = []
        self.deliver_self_wake = None
        self._status = status
        self._orch = SimpleNamespace(_room_replied_ids=["7"])

    async def create_session(self, **kwargs):
        self.calls.append(kwargs)
        return {"id": "s-room"}

    async def run_session(self, user_id, session_id):
        return self._status

    def get_orchestrator(self, sid):
        return self._orch


def _row(mid, text, ts):
    return LedgerRow("telegram", "-1001", None, str(mid), ts, "8123", "@alice",
                     False, "member", "text", text, None, False)


def _goal():
    return Goal(id="g-room", user_id="rob", title="Service room The Den",
                payload={"group": {"surface": "telegram", "chat_id": "-1001"},
                         "tools": ["defi_trade"], "max_steps": 8})


def _dispatch(tmp_path, monkeypatch, *, rows=(("7", "is the bridge live?", NOW - 30),)):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    container = _Container(tmp_path)
    ledger = container.get_service("group_ledger")
    for mid, text, ts in rows:
        ledger.append(_row(mid, text, ts))
    board = _FakeBoard()
    agent = _TaskAgent(container)
    disp = GoalDispatcher(board, agent)
    asyncio.run(disp._run_goal(_goal()))
    return agent, board, ledger


def test_create_session_is_bound_to_the_room(tmp_path, monkeypatch):
    agent, _board, _lg = _dispatch(tmp_path, monkeypatch)
    assert agent.calls, "the goal never reached create_session"
    kwargs = agent.calls[0]
    src = kwargs.get("session_source")
    assert src is not None and src.chat_id == "-1001"
    assert src.surface_id == "telegram" and src.chat_type == "supergroup"
    assert kwargs.get("chat_session_key") == "agent:main:telegram:supergroup:-1001"


def test_the_room_toolset_wins_over_payload_tools(tmp_path, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING):
        agent, _board, _lg = _dispatch(tmp_path, monkeypatch)
    request = agent.calls[0]["request"]
    assert request["tools"] == room_tool_ids()
    assert "defi_trade" not in request["tools"]
    assert any("payload.tools ignored" in r.getMessage() for r in caplog.records)


def test_the_turn_text_is_the_room_tail(tmp_path, monkeypatch):
    agent, _board, _lg = _dispatch(tmp_path, monkeypatch)
    task = agent.calls[0]["request"]["task"]
    assert "is the bridge live?" in task and "At most" in task


def test_an_empty_tail_never_creates_a_session(tmp_path, monkeypatch):
    """$0: nothing was asked, so no model is paid to discover that."""
    agent, board, _lg = _dispatch(tmp_path, monkeypatch, rows=())
    assert agent.calls == []
    assert board.successes == ["g-room"]
    assert "no_change" in (board.results[0] or "")


def test_the_checkpoint_advances_after_the_run(tmp_path, monkeypatch):
    _agent, _board, ledger = _dispatch(tmp_path, monkeypatch)
    assert ledger.checkpoint("telegram", "-1001", "goal") == NOW - 30
    # The id the run replied to (orchestrator._room_replied_ids) is marked.
    assert ledger.tail("telegram", "-1001", unanswered_only=True) == []


# ---------------------------------------------------------------------------
# Fix round 1
# ---------------------------------------------------------------------------

def test_the_run_binds_without_writing_the_registry_row(tmp_path, monkeypatch):
    """Critical 1: `bind_chat_surface` upserts on session_key, so each tick would
    re-point the room key at its own one-shot session — the next human line would
    land inside the SERVICE rubric (an owner answered with `[SILENT]`), the real
    room session would be orphaned, and the refreshed `updated_at` would keep the
    idle reset from ever firing."""
    agent, _board, _lg = _dispatch(tmp_path, monkeypatch)
    assert agent.calls[0].get("bind_write_row") is False
    # ...and the id is pre-generated so the `finally` can find the orchestrator.
    assert agent.calls[0].get("session_id")


def test_bind_chat_surface_write_row_false_leaves_the_live_row_alone(tmp_path, monkeypatch):
    """The behaviour the flag buys, at the seam itself."""
    from core.sqlite_util import execute_retry
    from core.surfaces.binding import bind_chat_surface
    from core.surfaces.envelopes import SessionSource
    from core.surfaces.session_chat_registry import SessionChatRegistry

    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    db = str(tmp_path / "surfaces.db")
    registry = SessionChatRegistry(db)
    key = "agent:main:telegram:supergroup:-1001"
    src = SessionSource(surface_id="telegram", chat_id="-1001", chat_type="supergroup")
    registry.bind(key, "live-session", "rob", "telegram", "-1001")
    # `updated_at` is second-resolution, so pin a known old value rather than
    # racing the clock — the idle reset reads exactly this column.
    execute_retry(db, "UPDATE session_chat_map SET updated_at=1 WHERE session_key=?", (key,))
    before = registry.resolve(key)

    container = SimpleNamespace(get_service=lambda n: {
        "message_router": SimpleNamespace(), "session_chat_registry": registry}.get(n))
    orch = SimpleNamespace()
    assert bind_chat_surface(orch, container, session_source=src, chat_session_key=key,
                             session_id="service-session", user_id="rob",
                             write_row=False) is True
    assert orch._public_session is True, "the service run must still be PUBLIC"
    assert orch._chat_session_key == key
    after = registry.resolve(key)
    assert after["session_id"] == "live-session" == before["session_id"]
    assert after["updated_at"] == before["updated_at"] == 1

    # ...while the default still takes the row (every ordinary chat turn).
    bind_chat_surface(orch, container, session_source=src, chat_session_key=key,
                      session_id="another-session", user_id="rob")
    assert registry.resolve(key)["session_id"] == "another-session"


def test_a_muted_room_is_a_typed_skip(tmp_path, monkeypatch):
    """Important 2: the skip reason reaches the board note, so `muted` and
    `no_change` are never the same line in the log."""
    from core.surfaces import chat_policy
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    ok, msg = chat_policy.set(str(tmp_path), "rob", "telegram", "-1001",
                              "chat.mode", "off")
    assert ok, msg
    agent, board, _lg = _dispatch(tmp_path, monkeypatch)
    assert agent.calls == []
    assert "mode_off" in (board.results[0] or "")


def test_a_timed_out_run_still_marks_what_it_answered(tmp_path, monkeypatch):
    """Important 5: the scheduler's wall-clock cap CANCELS the run, so the old
    post-run bookkeeping never fired and the next tick re-answered every line the
    timed-out run had already answered."""
    import asyncio as _aio
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    container = _Container(tmp_path)
    ledger = container.get_service("group_ledger")
    ledger.append(_row("7", "is the bridge live?", NOW - 30))
    board = _FakeBoard()
    agent = _TaskAgent(container)

    async def _boom(*a, **kw):
        raise _aio.TimeoutError()

    monkeypatch.setattr("agents.task.goals.dispatcher._run_task_to_outcome", _boom)
    disp = GoalDispatcher(board, agent)
    asyncio.run(disp._run_goal(_goal()))
    assert board.failures, "a timeout is still a run failure"
    assert ledger.checkpoint("telegram", "-1001", "goal") > 0
    assert ledger.tail("telegram", "-1001", unanswered_only=True) == []


def test_the_books_close_against_the_runs_real_session_id(tmp_path, monkeypatch):
    """N2 (fix round 2): the pre-generated id is only honoured if create_session
    took it. When the run reports a different one, THAT is the session that
    answered — `answered_by` must name it."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    container = _Container(tmp_path)
    ledger = container.get_service("group_ledger")
    ledger.append(_row("7", "is the bridge live?", NOW - 30))

    class _OtherId(_TaskAgent):
        async def create_session(self, **kwargs):
            self.calls.append(kwargs)
            return {"id": "an-entirely-different-id"}

        def get_orchestrator(self, sid):
            assert sid == "an-entirely-different-id"
            return self._orch

    agent = _OtherId(container)
    asyncio.run(GoalDispatcher(_FakeBoard(), agent)._run_goal(_goal()))
    rows = ledger.tail("telegram", "-1001", limit=10)
    assert [r.answered_by for r in rows] == ["an-entirely-different-id"]
