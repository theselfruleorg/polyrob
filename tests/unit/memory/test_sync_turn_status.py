"""D4 (2026-10-13 memory/coherency review): at the tenant storage cap every
insert raised, only a warning was logged, and the ``memory_write`` event still
said the finding was written. ``memory_sync_turn`` now returns what happened,
the event fires only on a real insert, and a full store records ONE
owner-visible verdict a day (``core.credential_verdicts`` → /status)."""
import logging
from types import SimpleNamespace

import pytest

from modules.memory import registry as R
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider


@pytest.fixture
def prov(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "false")
    monkeypatch.setenv("MEMORY_STORE_ANSWER_ONLY", "true")
    R.reset_memory_registry()
    p = SqliteMemoryProvider(str(tmp_path / "memory.db"))
    R.set_external_memory_provider(p)
    R._store_full.update(recorded_at=0.0, open=None)
    yield p
    R.reset_memory_registry()


@pytest.fixture
def verdicts(monkeypatch):
    calls = {"record": [], "clear": []}
    import core.credential_verdicts as cv
    monkeypatch.setattr(cv, "record_rejection",
                        lambda kind, key="", **kw: calls["record"].append((kind, key, kw)))
    monkeypatch.setattr(cv, "clear_rejection", lambda kind, key="": calls["clear"].append((kind, key)))
    monkeypatch.setattr(cv, "verdict", lambda kind, key="": None)
    return calls


@pytest.mark.asyncio
async def test_statuses(prov, verdicts):
    assert await R.memory_sync_turn("q", "Bridge arrived on Base with 0.5 ETH.",
                                    session_id="s", user_id="u") == R.SYNC_INSERTED
    assert await R.memory_sync_turn("q", "Bridge arrived on Base with 0.5 ETH.",
                                    session_id="s", user_id="u") == R.SYNC_DUPLICATE
    assert await R.memory_sync_turn("q", "   ", session_id="s",
                                    user_id="u") == R.SYNC_SKIPPED


@pytest.mark.asyncio
async def test_storage_cap_records_one_verdict_a_day(prov, verdicts, monkeypatch):
    import modules.memory.storage_limits as sl
    monkeypatch.setattr(sl, "MAX_MEMORY_ROWS", 1)
    assert await R.memory_sync_turn("q", "First finding about the treasury.",
                                    session_id="s", user_id="u") == R.SYNC_INSERTED
    for i in range(3):
        got = await R.memory_sync_turn("q", f"Owner correction number {i} about rebuys.",
                                       session_id="s", user_id="u")
        assert got == R.SYNC_REFUSED_CAP
    assert len(verdicts["record"]) == 1
    kind, key, kw = verdicts["record"][0]
    assert kind == R.STORE_FULL_KIND and "NOT" in kw["remedy"]
    # room again (the owner pruned): a real insert clears the standing verdict
    monkeypatch.setattr(sl, "MAX_MEMORY_ROWS", 100)
    assert await R.memory_sync_turn("q", "A new finding after the prune.",
                                    session_id="s", user_id="u") == R.SYNC_INSERTED
    assert verdicts["clear"] == [(R.STORE_FULL_KIND, R.STORE_FULL_KEY)]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,events", [(R.SYNC_INSERTED, 1), (R.SYNC_REFUSED_CAP, 0),
                                           (R.SYNC_DUPLICATE, 0), (R.SYNC_FAILED, 0)])
async def test_memory_write_event_only_on_insert(monkeypatch, status, events):
    from agents.task.agent.core.memory_writer import MemoryWriterMixin

    async def fake_sync(*a, **k):
        return status
    emitted = []
    monkeypatch.setattr("modules.memory.registry.memory_sync_turn", fake_sync)
    monkeypatch.setattr("agents.task.telemetry.memory_events.emit_memory_event",
                        lambda kind, **kw: emitted.append(kind) or None)
    host = MemoryWriterMixin()
    host.orchestrator = None
    released = []
    host.task_context_manager = SimpleNamespace(
        add_step_memory=lambda **kw: True, get_session=lambda sid: None,
        drain_promoted_findings=lambda sid: ["a finding"],
        release_unsent_findings=lambda sid, f, uid=None: released.append(list(f)) or True)
    host.state = SimpleNamespace(track_finding=lambda: None)
    host.session_id, host.user_id, host.task = "s", "owner", "task"
    host.logger = logging.getLogger("d4")
    await host._save_step_to_memory(step_number=1, brain_state={"memory": "a finding"},
                                    actions=[], results=[])
    assert emitted.count("memory_write") == events
    # only a failed / refused write gives the findings back for a retry
    assert released == ([["a finding"]] if status in (R.SYNC_FAILED, R.SYNC_REFUSED_CAP) else [])


@pytest.mark.asyncio
async def test_store_full_again_after_a_clear_is_recorded_at_once(prov, verdicts, monkeypatch):
    """Re-validation: the once-a-day limit kept a SECOND fill (after an insert
    cleared the first verdict) silent for the rest of the day."""
    import modules.memory.storage_limits as sl
    monkeypatch.setattr(sl, "MAX_MEMORY_ROWS", 1)
    await R.memory_sync_turn("q", "First finding about the treasury.", session_id="s", user_id="u")
    assert await R.memory_sync_turn("q", "Second finding about rebuys.",
                                    session_id="s", user_id="u") == R.SYNC_REFUSED_CAP
    monkeypatch.setattr(sl, "MAX_MEMORY_ROWS", 2)
    assert await R.memory_sync_turn("q", "Second finding about rebuys.",
                                    session_id="s", user_id="u") == R.SYNC_INSERTED
    assert await R.memory_sync_turn("q", "Third finding about the den bounty.",
                                    session_id="s", user_id="u") == R.SYNC_REFUSED_CAP
    assert len(verdicts["record"]) == 2
