"""W3 — cron runner now RUNS the agent loop, and delivers results.

The live bug: ``make_agent_runner.runner`` called ``create_session`` (which only
builds the orchestrator) and returned ``bool(session_info)`` — the step loop
(``run_session``) was never entered, so every cron job did nothing. These tests pin
the fix and the new opt-in delivery, including the safety gates.
"""
import os

import pytest

from cron.runner import make_agent_runner
from cron import delivery as cron_delivery
from cron.jobs import CronJob


def _job(**kw):
    base = dict(
        id="j1", task="do the thing", schedule_spec="30m", user_id="u1",
        next_run_at=None, one_shot=True, skip_memory=True, max_duration_seconds=180,
        payload=kw.pop("payload", {}), created_at=None,
    )
    base.update(kw)
    return CronJob(**base)


class _FakeTaskAgent:
    def __init__(self, final="done"):
        self.created = False
        self.ran_with = None
        self._final = final

    async def create_session(self, *, user_id, request):
        self.created = True
        return {"id": "sess-1"}

    async def run_session(self, user_id, session_id):
        self.ran_with = (user_id, session_id)
        return self._final


@pytest.mark.asyncio
async def test_runner_invokes_run_session_when_cron_run_loop_on(monkeypatch):
    monkeypatch.setenv("CRON_RUN_LOOP", "true")
    agent = _FakeTaskAgent(final="result text")
    runner = make_agent_runner(agent)
    ok = await runner(_job())
    assert ok is True
    assert agent.ran_with == ("u1", "sess-1"), "run_session MUST be called (the bug fix)"


@pytest.mark.asyncio
async def test_runner_treats_refusal_string_as_failure(monkeypatch):
    """run_session returns truthy REFUSAL strings ('No active session found', etc.);
    the runner must NOT report those as success or deliver them."""
    monkeypatch.setenv("CRON_RUN_LOOP", "true")
    agent = _FakeTaskAgent(final="No active session found")
    runner = make_agent_runner(agent)
    ok = await runner(_job())
    assert ok is False


@pytest.mark.asyncio
async def test_runner_no_run_loop_when_disabled(monkeypatch):
    monkeypatch.setenv("CRON_RUN_LOOP", "false")
    agent = _FakeTaskAgent()
    runner = make_agent_runner(agent)
    ok = await runner(_job())
    assert ok is True  # legacy behaviour: session built
    assert agent.ran_with is None  # loop NOT entered


@pytest.mark.asyncio
async def test_delivery_fires_when_enabled(monkeypatch):
    monkeypatch.setenv("CRON_RUN_LOOP", "true")
    monkeypatch.setenv("CRON_DELIVERY_ENABLED", "true")
    calls = {}

    async def fake_deliver(task_agent, job, final, *, target, deliver_target=None,
                           session_id=None):
        calls["target"] = target
        calls["final"] = final
        calls["session_id"] = session_id
        return True

    monkeypatch.setattr(cron_delivery, "deliver_result", fake_deliver)
    agent = _FakeTaskAgent(final="hello")
    runner = make_agent_runner(agent)
    await runner(_job(payload={"deliver": "email"}))
    assert calls["target"] == "email"
    assert calls["final"] == "hello"
    assert calls["session_id"] == "sess-1"  # Task 7: threaded through for surfaced-marking


@pytest.mark.asyncio
async def test_delivery_skipped_when_flag_off(monkeypatch):
    monkeypatch.setenv("CRON_RUN_LOOP", "true")
    monkeypatch.setenv("CRON_DELIVERY_ENABLED", "false")
    fired = {"x": False}

    async def fake_deliver(*a, **k):
        fired["x"] = True
        return True

    monkeypatch.setattr(cron_delivery, "deliver_result", fake_deliver)
    runner = make_agent_runner(_FakeTaskAgent())
    await runner(_job(payload={"deliver": "email"}))
    assert fired["x"] is False


# --- deliver_result unit gates ----------------------------------------------

@pytest.mark.asyncio
async def test_deliver_rejects_unknown_target():
    assert await cron_delivery.deliver_result(None, _job(), "x", target="pager") is False


@pytest.mark.asyncio
async def test_deliver_suppressed_by_silent_marker():
    assert await cron_delivery.deliver_result(None, _job(), "all good [SILENT]", target="email") is False


@pytest.mark.asyncio
async def test_deliver_blank_result_is_noop():
    assert await cron_delivery.deliver_result(None, _job(), "   ", target="email") is False


@pytest.mark.asyncio
async def test_deliver_sink_exception_is_swallowed(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("smtp down")
    monkeypatch.setattr(cron_delivery, "_deliver_email", boom)
    # fail-open: returns False, does not raise
    assert await cron_delivery.deliver_result(object(), _job(), "real result", target="email") is False


@pytest.mark.asyncio
async def test_deliver_target_ignored_by_default(monkeypatch):
    """Exfil guard: an agent-supplied deliver_target is ignored unless the operator
    opted in — delivery falls back to owner-only (deliver_target=None)."""
    monkeypatch.delenv("CRON_DELIVERY_ALLOW_EXPLICIT_TARGET", raising=False)
    seen = {}

    async def fake_email(task_agent, job, final, deliver_target):
        seen["deliver_target"] = deliver_target
        return True

    monkeypatch.setattr(cron_delivery, "_deliver_email", fake_email)
    await cron_delivery.deliver_result(object(), _job(), "result",
                                       target="email", deliver_target="attacker@evil.com")
    assert seen["deliver_target"] is None  # explicit target dropped


@pytest.mark.asyncio
async def test_deliver_target_honored_when_opted_in(monkeypatch):
    monkeypatch.setenv("CRON_DELIVERY_ALLOW_EXPLICIT_TARGET", "true")
    seen = {}

    async def fake_email(task_agent, job, final, deliver_target):
        seen["deliver_target"] = deliver_target
        return True

    monkeypatch.setattr(cron_delivery, "_deliver_email", fake_email)
    await cron_delivery.deliver_result(object(), _job(), "result",
                                       target="email", deliver_target="me@mine.com")
    assert seen["deliver_target"] == "me@mine.com"


# --- Task 7: surfaced-marking on successful delivery ------------------------

@pytest.mark.asyncio
async def test_deliver_success_marks_episode_surfaced(monkeypatch):
    async def fake_email(task_agent, job, final, deliver_target):
        return True
    monkeypatch.setattr(cron_delivery, "_deliver_email", fake_email)

    marked = {}

    class _FakeProvider:
        def mark_episode_surfaced(self, *, session_id, user_id=None):
            marked["session_id"] = session_id
            marked["user_id"] = user_id

    class _FakeRegistry:
        def active(self):
            return _FakeProvider()

    monkeypatch.setattr(
        "modules.memory.registry.get_memory_registry", lambda: _FakeRegistry())
    ok = await cron_delivery.deliver_result(
        object(), _job(), "result", target="email", session_id="sess-xyz")
    assert ok is True
    assert marked["session_id"] == "sess-xyz"
    # FIX2: tenant-scoped -- the job's own user_id ("u1" per the _job() fixture)
    # must be threaded through so a session_id collision can't flip another
    # tenant's row.
    assert marked["user_id"] == "u1"


@pytest.mark.asyncio
async def test_deliver_failure_does_not_mark_surfaced(monkeypatch):
    async def fake_email(task_agent, job, final, deliver_target):
        return False
    monkeypatch.setattr(cron_delivery, "_deliver_email", fake_email)

    marked = {"called": False}

    class _FakeProvider:
        def mark_episode_surfaced(self, *, session_id, user_id=None):
            marked["called"] = True

    class _FakeRegistry:
        def active(self):
            return _FakeProvider()

    monkeypatch.setattr(
        "modules.memory.registry.get_memory_registry", lambda: _FakeRegistry())
    ok = await cron_delivery.deliver_result(
        object(), _job(), "result", target="email", session_id="sess-xyz")
    assert ok is False
    assert marked["called"] is False


@pytest.mark.asyncio
async def test_deliver_surfaced_mark_is_fail_open(monkeypatch):
    """A crashing memory registry must never turn a successful delivery into a
    reported failure."""
    async def fake_email(task_agent, job, final, deliver_target):
        return True
    monkeypatch.setattr(cron_delivery, "_deliver_email", fake_email)
    monkeypatch.setattr(
        "modules.memory.registry.get_memory_registry",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    ok = await cron_delivery.deliver_result(
        object(), _job(), "result", target="email", session_id="sess-xyz")
    assert ok is True


def test_is_silent():
    assert cron_delivery.is_silent("nothing [silent] here") is True
    assert cron_delivery.is_silent("normal") is False
    assert cron_delivery.is_silent(None) is False


def test_delivery_outcome_distinguishes_suppressed_from_failed():
    # A [SILENT] opt-out is NOT a failure — the runner used to log both as ok=False,
    # so 40 harmless status-digest opt-outs looked like 40 broken deliveries (2026-07-03).
    assert cron_delivery.delivery_outcome("[SILENT] nothing new", ok=False) == "suppressed"
    assert cron_delivery.delivery_outcome("here is your digest", ok=True) == "sent"
    assert cron_delivery.delivery_outcome("here is your digest", ok=False) == "failed"
