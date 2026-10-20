"""Re-validation 2026-10-13 (R2/R3): an unreadable episode store must render
<previous-runs> "could not read", never an empty block that reads as a first
run. memory_recall_episodes kept its [] default for every other caller."""
import pytest

import modules.memory.registry as reg
import modules.memory.sqlite_episodes_store as store_mod
from agents.task.agent.core.episodic_digest import render_previous_runs
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider


@pytest.fixture
def broken(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "true")
    p = SqliteMemoryProvider(str(tmp_path / "memory.db"))
    reg.reset_memory_registry()
    reg.set_external_memory_provider(p)

    def _boom(*a, **k):
        raise RuntimeError("database disk image is malformed")
    monkeypatch.setattr(store_mod, "execute_retry", _boom)
    yield p
    reg.reset_memory_registry()


@pytest.mark.asyncio
async def test_default_callers_still_get_an_empty_list(broken):
    assert await reg.memory_recall_episodes(user_id="u1", limit=5) == []


@pytest.mark.asyncio
async def test_strict_callers_get_a_read_error(broken):
    with pytest.raises(reg.MemoryReadError):
        await reg.memory_recall_episodes(user_id="u1", limit=5, raise_errors=True)


@pytest.mark.asyncio
async def test_previous_runs_says_could_not_read(broken):
    out = await render_previous_runs(user_id="u1", rail_id="cron:j1", regime="cron")
    assert "Could not read this job's earlier runs" in out
    assert "Do not assume this is the first run" in out
