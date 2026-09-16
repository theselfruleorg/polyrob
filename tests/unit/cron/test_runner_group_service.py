"""044 T20: a cron job carrying ``payload.group`` SERVICES a room.

The service job is a CRON job, not a goal — it is the one recurring path, and
``/groups service here every 30m`` is a cadence, not a one-off backlog item.

Two behaviours are pinned:

- an EMPTY (or bot-only) tail is a ``$0`` tick: a ``skipped/no_change`` event and
  NO session — the same shape the wake change-gate already uses. Paying a model
  to discover that nobody said anything is the cost this whole rail exists to
  avoid;
- a non-empty tail binds the run to the ROOM session and runs it with the room
  toolset, ignoring any ``payload.tools``.
"""
import time

import pytest

from core.surfaces.group_ledger import GroupLedger, LedgerRow
from core.surfaces.room_policy import room_tool_ids
from cron.jobs import CronJob
from cron.runner import make_agent_runner

#: ⚠️ Ledger rows prune by WALL CLOCK, so every fixture ts is relative to now.
NOW = time.time()


def _job(**kw):
    base = dict(
        id="j-room", task="Service room The Den", schedule_spec="30m",
        user_id="rob", next_run_at=None, one_shot=False, skip_memory=True,
        max_duration_seconds=180,
        payload=kw.pop("payload", {"group": {"surface": "telegram",
                                             "chat_id": "-1001"}}),
        created_at=None,
    )
    base.update(kw)
    return CronJob(**base)


class _Container:
    def __init__(self, tmp_path):
        import types
        self.ledger = GroupLedger(str(tmp_path / "surfaces.db"))
        self.config = types.SimpleNamespace(data_dir=str(tmp_path))

    def get_service(self, n):
        return {"group_ledger": self.ledger}.get(n)


class _FakeTaskAgent:
    def __init__(self, container, final="answered"):
        self.container = container
        self.calls = []
        self._final = final
        import types
        self._orch = types.SimpleNamespace(_room_replied_ids=["7"])

    async def create_session(self, **kwargs):
        self.calls.append(kwargs)
        return {"id": "sess-room"}

    async def run_session(self, user_id, session_id):
        return self._final

    def get_orchestrator(self, sid):
        return self._orch


def _row(mid, text, ts, bot=False):
    return LedgerRow("telegram", "-1001", None, str(mid), ts, "8123", "@alice",
                     bot, "member", "text", text, None, False)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("CRON_RUN_LOOP", "true")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    import core.event_log as el
    monkeypatch.setattr(el, "_INSTANCES", {})
    log = el.TelemetryEventLog(str(tmp_path / "te.db"))
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: log)
    monkeypatch.setattr(el, "event_log_enabled", lambda: True)
    container = _Container(tmp_path)
    agent = _FakeTaskAgent(container)
    return agent, container, log, str(tmp_path)


@pytest.mark.asyncio
async def test_empty_tail_is_a_zero_cost_skip(rig):
    agent, _container, log, data_dir = rig
    runner = make_agent_runner(agent, data_dir=data_dir)
    ok = await runner(_job())
    assert ok is True
    assert agent.calls == [], "a $0 skip must never create a session"
    rows = log.query(kind="cron_run")
    assert [(r["attrs"]["outcome"], r["attrs"]["reason"]) for r in rows] \
        == [("skipped", "no_change")]


@pytest.mark.asyncio
async def test_bot_only_tail_is_also_a_skip(rig):
    agent, container, _log, data_dir = rig
    container.ledger.append(_row(1, "beep boop", NOW - 60, bot=True))
    runner = make_agent_runner(agent, data_dir=data_dir)
    assert await runner(_job()) is True
    assert agent.calls == []


@pytest.mark.asyncio
async def test_a_real_tail_binds_the_run_to_the_room(rig):
    agent, container, _log, data_dir = rig
    container.ledger.append(_row(7, "is the bridge live?", NOW - 30))
    runner = make_agent_runner(agent, data_dir=data_dir)
    assert await runner(_job()) is True
    kwargs = agent.calls[0]
    assert kwargs["session_source"].chat_id == "-1001"
    assert kwargs["chat_session_key"] == "agent:main:telegram:supergroup:-1001"
    assert kwargs["request"]["tools"] == room_tool_ids()
    assert "is the bridge live?" in kwargs["request"]["task"]


@pytest.mark.asyncio
async def test_payload_tools_are_ignored_for_a_room_job(rig):
    agent, container, _log, data_dir = rig
    container.ledger.append(_row(7, "hello?", NOW - 30))
    runner = make_agent_runner(agent, data_dir=data_dir)
    await runner(_job(payload={"group": {"surface": "telegram", "chat_id": "-1001"},
                               "tools": ["defi_trade"]}))
    assert "defi_trade" not in agent.calls[0]["request"]["tools"]


@pytest.mark.asyncio
async def test_the_checkpoint_advances_after_the_run(rig):
    agent, container, _log, data_dir = rig
    container.ledger.append(_row(7, "is the bridge live?", NOW - 30))
    runner = make_agent_runner(agent, data_dir=data_dir)
    await runner(_job())
    assert container.ledger.checkpoint("telegram", "-1001", "goal") == NOW - 30
    assert container.ledger.tail("telegram", "-1001", unanswered_only=True) == []


@pytest.mark.asyncio
async def test_a_plain_cron_job_is_untouched(rig):
    """No ``payload.group`` ⇒ byte-identical to a normal cron run."""
    agent, _container, _log, data_dir = rig
    runner = make_agent_runner(agent, data_dir=data_dir)
    assert await runner(_job(payload={})) is True
    kwargs = agent.calls[0]
    assert "session_source" not in kwargs and "chat_session_key" not in kwargs
    assert kwargs["request"]["task"] == "Service room The Den"


# ---------------------------------------------------------------------------
# Fix round 1
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_run_binds_without_taking_the_registry_row(rig):
    """Critical 1: a service tick must not re-point the room key at its own
    one-shot session."""
    agent, container, _log, data_dir = rig
    container.ledger.append(_row(7, "anyone?", NOW - 30))
    await make_agent_runner(agent, data_dir=data_dir)(_job())
    assert agent.calls[0].get("bind_write_row") is False
    assert agent.calls[0].get("session_id")


@pytest.mark.asyncio
async def test_a_muted_room_is_a_typed_skip_event(rig, monkeypatch):
    """Important 2: `skipped/muted` and `skipped/no_change` are different facts
    and must not read the same in the event log."""
    agent, container, log, data_dir = rig
    container.ledger.append(_row(7, "anyone?", NOW - 30))
    from core.surfaces import chat_policy
    ok, msg = chat_policy.set(data_dir, "rob", "telegram", "-1001",
                              "chat.mute_until", NOW + 3600)
    assert ok, msg
    assert await make_agent_runner(agent, data_dir=data_dir)(_job()) is True
    assert agent.calls == []
    rows = log.query(kind="cron_run")
    assert [(r["attrs"]["outcome"], r["attrs"]["reason"]) for r in rows] \
        == [("skipped", "muted")]


@pytest.mark.asyncio
async def test_the_rooms_own_max_reaches_the_rubric(rig):
    """Important 3: `/groups service here max 1` wrote the number and it never
    reached the run."""
    agent, container, _log, data_dir = rig
    container.ledger.append(_row(7, "anyone?", NOW - 30))
    await make_agent_runner(agent, data_dir=data_dir)(_job(payload={
        "group": {"surface": "telegram", "chat_id": "-1001"}, "max_replies": 1}))
    assert "At most 1 reply" in agent.calls[0]["request"]["task"]


@pytest.mark.asyncio
async def test_a_cancelled_run_still_closes_the_books(rig, monkeypatch):
    """Important 5: the scheduler CANCELS the runner on its per-job cap, so the
    old post-run bookkeeping never fired and the next tick re-answered."""
    import asyncio
    agent, container, _log, data_dir = rig
    container.ledger.append(_row(7, "is the bridge live?", NOW - 30))

    async def _hang(*a, **kw):
        await asyncio.sleep(10)

    monkeypatch.setattr("cron.runner._run_task_to_outcome", _hang)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(make_agent_runner(agent, data_dir=data_dir)(_job()),
                               timeout=0.05)
    assert container.ledger.checkpoint("telegram", "-1001", "goal") > 0
    assert container.ledger.tail("telegram", "-1001", unanswered_only=True) == []
