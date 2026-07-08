"""Owner daily digest — deterministic composition + delivery routing."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cron import digest


@pytest.mark.asyncio
async def test_compose_digest_includes_money_and_activity(monkeypatch):
    monkeypatch.setattr(digest, "_ledger", lambda uid, days: {
        "earned_usd": 2.0, "total_spend_usd": 0.5, "net_usd": 1.5,
        "pending_invoices": 1, "pending_invoices_usd": 3.0})
    monkeypatch.setattr(digest, "_event_aggregate", lambda uid, since: {
        "counts_by_kind": {"goal_run": 2, "self_modification": 1}, "total_events": 3})
    monkeypatch.setattr(digest, "_open_asks", lambda uid, data_dir: [{"title": "unblock me"}])
    monkeypatch.setattr(digest, "_episodes", lambda uid, since: [
        {"kind": "cron", "outcome": "done"}])
    text = await digest.compose_digest("u1", days=1)
    assert "$2.00" in text and "earned" in text.lower()
    assert "net $1.50" in text
    assert "1 pending invoice" in text
    assert "2 goal event" in text and "1 self-change" in text
    assert "1 session" in text
    assert "unblock me" in text


@pytest.mark.asyncio
async def test_compose_digest_all_empty_is_stable(monkeypatch):
    monkeypatch.setattr(digest, "_ledger", lambda uid, days: {})
    monkeypatch.setattr(digest, "_event_aggregate", lambda uid, since: {})
    monkeypatch.setattr(digest, "_open_asks", lambda uid, data_dir: [])
    monkeypatch.setattr(digest, "_episodes", lambda uid, since: [])
    text = await digest.compose_digest("u1", days=1)
    assert "Daily digest — today" in text
    assert "earned $0.00" in text
    assert "Pending approvals: none" in text


@pytest.mark.asyncio
async def test_run_digest_delivers_composed_text(monkeypatch):
    monkeypatch.setattr(digest, "compose_digest",
                        AsyncMock(return_value="DIGEST BODY"))
    sent = {}

    async def _deliver(task_agent, job, final, *, target, deliver_target=None, session_id=None):
        sent["final"] = final
        sent["target"] = target
        return True

    monkeypatch.setattr("cron.delivery.deliver_result", _deliver)
    job = SimpleNamespace(id="j1", user_id="u1", payload={"digest": True, "deliver": "telegram"})
    ok = await digest.run_digest(object(), job)
    assert ok is True
    assert sent["final"] == "DIGEST BODY"
    assert sent["target"] == "telegram"


@pytest.mark.asyncio
async def test_run_digest_fail_open_on_compose_error(monkeypatch):
    monkeypatch.setattr(digest, "compose_digest",
                        AsyncMock(side_effect=RuntimeError("boom")))
    job = SimpleNamespace(id="j1", user_id="u1", payload={"digest": True})
    assert await digest.run_digest(object(), job) is False


def test_owner_digest_flag(monkeypatch):
    from agents.task.constants import AutonomyConfig
    monkeypatch.delenv("OWNER_DIGEST_ENABLED", raising=False)
    assert AutonomyConfig.owner_digest_enabled() is False
    monkeypatch.setenv("OWNER_DIGEST_ENABLED", "true")
    assert AutonomyConfig.owner_digest_enabled() is True


@pytest.mark.asyncio
async def test_runner_routes_digest_payload_to_run_digest(monkeypatch):
    # A payload.digest job routes to run_digest and NEVER invokes the model.
    monkeypatch.setenv("OWNER_DIGEST_ENABLED", "true")
    from cron.runner import make_agent_runner
    from tests.unit.cron.test_runner_runloop_delivery import _job

    calls = {"create": 0, "run": 0, "digest": 0}

    class _Agent:
        async def create_session(self, **kw):
            calls["create"] += 1
            return {"session_id": "s1"}

        async def run_session(self, *a, **k):
            calls["run"] += 1
            return "x"

    async def _fake_run_digest(task_agent, job):
        calls["digest"] += 1
        return True

    monkeypatch.setattr("cron.digest.run_digest", _fake_run_digest)
    runner = make_agent_runner(_Agent())
    job = _job(payload={"digest": True, "deliver": "telegram"})
    ok = await runner(job)
    assert ok is True
    assert calls == {"create": 0, "run": 0, "digest": 1}


@pytest.mark.asyncio
async def test_runner_digest_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("OWNER_DIGEST_ENABLED", "false")
    from cron.runner import make_agent_runner
    from tests.unit.cron.test_runner_runloop_delivery import _job

    called = {"digest": 0}

    async def _fake_run_digest(task_agent, job):
        called["digest"] += 1
        return True

    monkeypatch.setattr("cron.digest.run_digest", _fake_run_digest)
    runner = make_agent_runner(object())
    job = _job(payload={"digest": True})
    ok = await runner(job)
    assert ok is True
    assert called["digest"] == 0  # disabled -> never composes/delivers


@pytest.mark.asyncio
async def test_ledger_seam_works_inside_running_loop(monkeypatch):
    # Regression: compose_digest is awaited inside the cron runner's running loop;
    # the sync _ledger seam must be loop-safe (bare asyncio.run would raise -> {}).
    async def _fake_build(user_id, *, days=7, db=None):
        return {"earned_usd": 9.0, "total_spend_usd": 2.0, "net_usd": 7.0}

    monkeypatch.setattr("modules.credits.unified_ledger.build_ledger", _fake_build)
    # we are already inside a running loop (pytest.mark.asyncio)
    result = digest._ledger("u1", 1)
    assert result.get("earned_usd") == 9.0
