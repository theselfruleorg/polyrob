"""031 T5: a paid cron tick and the digest consult the pause record."""
import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    for k in ("AUTONOMY_HALT", "DATA_ROOT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    return tmp_path


def _job(payload):
    from cron.jobs import CronJob
    return CronJob(id="j1", task="x", schedule_spec="30m", user_id="rob", next_run_at=None,
                   payload=payload, max_duration_seconds=180)


@pytest.mark.asyncio
async def test_full_pause_skips_paid_tick_and_digest(home, monkeypatch):
    from core import autonomy_control as ac
    from cron import runner as r
    events = []
    monkeypatch.setattr(r, "_cron_ev", lambda job, outcome, reason=None, **kw: events.append((outcome, reason)))
    ac.pause(str(home), via="test")
    run = r.make_agent_runner(task_agent=object(), data_dir=str(home))
    assert await run(_job({})) is True
    assert await run(_job({"digest": True})) is True
    assert ("skipped", "paused") in events and ("skipped", "paused_digest") in events


@pytest.mark.asyncio
async def test_cron_scope_keeps_digest(home, monkeypatch):
    from core import autonomy_control as ac
    from cron import runner as r
    events = []
    monkeypatch.setattr(r, "_cron_ev", lambda job, outcome, reason=None, **kw: events.append((outcome, reason)))
    monkeypatch.setattr("cron.digest.digest_enabled_for", lambda uid, d: False)
    ac.pause(str(home), scopes=("cron",), via="test")
    run = r.make_agent_runner(task_agent=object(), data_dir=str(home))
    await run(_job({"digest": True}))
    assert ("skipped", "digest_disabled") in events  # reached the digest branch, not the pause skip
    assert await run(_job({})) is True
    assert ("skipped", "paused") in events
