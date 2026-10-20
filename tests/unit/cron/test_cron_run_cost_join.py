"""2026-10-14 review D-4 / A-9 — a `cron_run` row joins to its cost.

0 of 2,985 prod `cron_run` rows carried a session id, none said whether the run
did anything, and the `cron_delivery` rows could not be joined to a run. The
terminal `cron_run` row now carries `session_id` (its own column), `useful` (the
ONE `did_something` predicate) and the run's token/$ totals from
`usage_records`; `cron_delivery` carries `session_id` beside `job_id`.
"""
import os
import sqlite3

import pytest

from agents.task.runtime.run_outcome import RunOutcome
from cron import delivery as cron_delivery
from cron.jobs import CronJob
from cron.runner import make_agent_runner


@pytest.fixture
def log(tmp_path, monkeypatch):
    import core.event_log as el
    monkeypatch.setattr(el, "_INSTANCES", {})
    test_log = el.TelemetryEventLog(str(tmp_path / "te.db"))
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: test_log)
    monkeypatch.setattr(el, "event_log_enabled", lambda: True)
    return test_log


def _bot_db(tmp_path, monkeypatch, rows):
    db = tmp_path / "database" / "bot.db"
    os.makedirs(db.parent, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE usage_records (id INTEGER PRIMARY KEY, user_id TEXT, "
                "session_id TEXT, input_tokens INTEGER, cached_tokens INTEGER, "
                "output_tokens INTEGER, api_cost_usd REAL, metadata TEXT, "
                "timestamp TEXT DEFAULT CURRENT_TIMESTAMP)")
    con.executemany("INSERT INTO usage_records (user_id, session_id, input_tokens, "
                    "cached_tokens, output_tokens, api_cost_usd, metadata) "
                    "VALUES (?,?,?,?,?,?,'{}')", rows)
    con.commit()
    con.close()
    monkeypatch.setenv("DB_PATH", str(db))


def _job(**kw):
    base = dict(id="j1", task="watch the pool", schedule_spec="30m", user_id="u1",
                next_run_at=None, one_shot=True, skip_memory=True,
                max_duration_seconds=180, payload=kw.pop("payload", {}), created_at=None)
    base.update(kw)
    return CronJob(**base)


class _Agent:
    async def create_session(self, *, user_id, request):
        return {"id": "sess-1"}


def _terminal(log):
    rows = [r for r in log.query(kind="cron_run")
            if r["attrs"].get("outcome") not in ("started",)]
    assert rows, log.query(kind="cron_run")
    return rows[-1]


@pytest.mark.asyncio
async def test_done_run_row_carries_session_useful_and_tokens(tmp_path, monkeypatch, log):
    monkeypatch.setenv("CRON_RUN_LOOP", "true")
    _bot_db(tmp_path, monkeypatch, [("u1", "sess-1", 1000, 800, 50, 0.01),
                                     ("u1", "sess-1", 2000, 1500, 70, 0.02),
                                     ("u1", "other", 9, 9, 9, 9.0)])

    async def fake_run(*a, **k):
        return RunOutcome(session_id="sess-1", status="ok", done_called=True,
                          done_text="nothing new", user_messages=[])
    monkeypatch.setattr("cron.runner._run_task_to_outcome", fake_run)
    assert await make_agent_runner(_Agent(), data_dir=str(tmp_path))(_job()) is True
    row = _terminal(log)
    assert row["session_id"] == "sess-1"
    a = row["attrs"]
    assert a["useful"] is False
    assert (a["llm_calls"], a["tokens_in"], a["tokens_cached"], a["tokens_out"]) == \
        (2, 3000, 2300, 120)
    assert a["cost_usd"] == pytest.approx(0.03)


@pytest.mark.asyncio
async def test_a_run_that_sent_is_useful(tmp_path, monkeypatch, log):
    monkeypatch.setenv("CRON_RUN_LOOP", "true")
    _bot_db(tmp_path, monkeypatch, [])

    async def fake_run(*a, **k):
        return RunOutcome(session_id="sess-1", status="ok", done_called=True,
                          done_text="told the owner", user_messages=["price moved 9%"])
    monkeypatch.setattr("cron.runner._run_task_to_outcome", fake_run)
    await make_agent_runner(_Agent(), data_dir=str(tmp_path))(_job())
    a = _terminal(log)["attrs"]
    assert a["useful"] is True
    assert a["llm_calls"] == 0          # a readable ledger with no row is a true 0


@pytest.mark.asyncio
async def test_unreadable_ledger_omits_totals_not_zero(tmp_path, monkeypatch, log):
    monkeypatch.setenv("CRON_RUN_LOOP", "true")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "absent" / "bot.db"))

    async def fake_run(*a, **k):
        return RunOutcome(session_id="sess-1", status="ok", done_called=True,
                          done_text="x", user_messages=[])
    monkeypatch.setattr("cron.runner._run_task_to_outcome", fake_run)
    await make_agent_runner(_Agent(), data_dir=str(tmp_path))(_job())
    a = _terminal(log)["attrs"]
    assert "tokens_in" not in a and "cost_usd" not in a
    assert "useful" in a


@pytest.mark.asyncio
async def test_delivery_row_carries_session_and_job(tmp_path, monkeypatch, log):
    monkeypatch.setenv("CRON_RUN_LOOP", "true")
    monkeypatch.setenv("CRON_DELIVERY_ENABLED", "true")
    _bot_db(tmp_path, monkeypatch, [])

    async def fake_deliver(task_agent, job, final, *, target, deliver_target=None,
                           session_id=None, record=None):
        return "sent"
    monkeypatch.setattr(cron_delivery, "deliver_result_ex", fake_deliver)

    async def fake_run(*a, **k):
        return RunOutcome(session_id="sess-1", status="ok", done_called=True,
                          done_text="x", user_messages=["hello"])
    monkeypatch.setattr("cron.runner._run_task_to_outcome", fake_run)
    await make_agent_runner(_Agent(), data_dir=str(tmp_path))(
        _job(payload={"deliver": "telegram"}))
    rows = log.query(kind="cron_delivery")
    assert rows and rows[-1]["session_id"] == "sess-1"
    assert rows[-1]["attrs"]["job_id"] == "j1"
