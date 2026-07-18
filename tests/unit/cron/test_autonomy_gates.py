"""G-35 (cron owner kill-switch).

Cron previously never honored ``AutonomyConfig.autonomy_halted()`` (goal
dispatch did, but ``cron/runner.py`` never referenced it). This pins the gate
onto ``make_agent_runner``'s existing skip-shape (mirrors
``cron/wake_gate``'s ``skipped/no_change``).

(The G-34 cron budget gate that used to live alongside this — a paid
``every 1m`` cron job billed unbounded regardless of ``AUTONOMY_BUDGET_USD`` —
was removed along with the autonomy budget gate itself; see Task 9 of the
money-ledger split proposal. A $/day RATE ceiling can't protect a finite
balance.)
"""
from unittest.mock import patch

import pytest

from cron.runner import make_agent_runner
from tests.unit.cron.test_runner_runloop_delivery import _job


@pytest.fixture(autouse=True)
def _clear_autonomy_marker():
    """Mirrors tests/unit/cron/test_runner.py — make_agent_runner marks its
    session autonomous in a module-global registry; without cleanup that
    leaks into later tests reusing the same session_id."""
    yield
    from agents.task.goals import autonomy_marker
    autonomy_marker._SESSIONS.clear()


class _Agent:
    def __init__(self):
        self.create_calls = 0
        self.run_calls = 0

    async def create_session(self, *, user_id, request):
        self.create_calls += 1
        return {"id": "sess-1"}

    async def run_session(self, user_id, session_id):
        self.run_calls += 1
        return "done"


def _events(monkeypatch, tmp_path):
    """Route _cron_ev at a real (tmp) event log so we can assert on outcome."""
    import agents.task.telemetry.event_log as el
    monkeypatch.setattr(el, "_INSTANCES", {})
    log = el.TelemetryEventLog(str(tmp_path / "te.db"))
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: log)
    monkeypatch.setattr(el, "event_log_enabled", lambda: True)
    return log


# --- G-35: owner kill-switch ------------------------------------------------

@pytest.mark.asyncio
async def test_halted_skips_without_invoking_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("CRON_RUN_LOOP", "true")
    monkeypatch.setenv("AUTONOMY_HALT", "true")
    log = _events(monkeypatch, tmp_path)
    agent = _Agent()
    runner = make_agent_runner(agent)
    ok = await runner(_job())
    assert ok is True  # a $0 skip is a success, not a failure
    assert agent.create_calls == 0
    assert agent.run_calls == 0
    rows = log.query(kind="cron_run")
    outcomes = [(r["attrs"]["outcome"], r["attrs"].get("reason")) for r in rows]
    assert ("skipped", "halted") in outcomes


@pytest.mark.asyncio
async def test_not_halted_runs_normally(monkeypatch):
    monkeypatch.setenv("CRON_RUN_LOOP", "true")
    monkeypatch.delenv("AUTONOMY_HALT", raising=False)
    agent = _Agent()
    runner = make_agent_runner(agent)
    ok = await runner(_job())
    assert ok is True
    assert agent.create_calls == 1
    assert agent.run_calls == 1


@pytest.mark.asyncio
async def test_halted_digest_job_stays_exempt(monkeypatch):
    """The digest branch returns before the halt check — an owner who halted
    autonomy still gets their own $0 status report, per the documented
    exemption in cron/runner.py."""
    monkeypatch.setenv("AUTONOMY_HALT", "true")
    agent = _Agent()
    runner = make_agent_runner(agent)
    with patch("cron.digest.digest_enabled_for", return_value=False):
        ok = await runner(_job(payload={"digest": True}))
    assert ok is True
    assert agent.create_calls == 0  # digest never invokes the agent anyway
