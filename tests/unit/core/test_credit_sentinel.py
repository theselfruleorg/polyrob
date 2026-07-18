"""§6.3 provider-credit sentinel — no more silent multi-day 402 grind.

Live evidence: 465× OpenRouter 402 on 2026-07-07, zero owner-facing signal,
autonomy effectively dead for two days. On a credit-death refusal from an
autonomous run the sentinel: sends ONE safety-net notice (via the §3.2 rail,
so it dedups), pauses goal dispatch + LLM cron ticks via a durable file latch,
and AUTO-RELEASES after CREDIT_SENTINEL_RELEASE_HOURS (one paid probe per
window, not a permanent manual halt).
"""
import asyncio
import json
import os
import time

import pytest


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    return tmp_path


def test_looks_like_credit_death_matches_provider_shapes():
    from core.credit_sentinel import looks_like_credit_death
    assert looks_like_credit_death(
        "Session failed: PERMANENT ERROR: Error code: 402 - insufficient credits") is True
    assert looks_like_credit_death("insufficient_quota: billing hard limit") is True
    assert looks_like_credit_death("Payment Required") is True
    assert looks_like_credit_death("Session failed: browser crashed") is False
    assert looks_like_credit_death(None) is False


def test_trip_activates_and_auto_releases(data_dir, monkeypatch):
    from core.credit_sentinel import (credit_sentinel_active, trip_credit_sentinel,
                                      _sentinel_path)
    assert credit_sentinel_active() is False
    asyncio.run(trip_credit_sentinel("openrouter 402", container=None, user_id="rob"))
    assert credit_sentinel_active() is True
    # age the latch past the release window → auto-release
    p = _sentinel_path()
    state = json.loads(open(p).read())
    state["ts"] = time.time() - 100 * 3600
    open(p, "w").write(json.dumps(state))
    assert credit_sentinel_active() is False
    assert not os.path.exists(p), "expired latch is removed (auto-release)"


def test_trip_sends_one_safety_net_notice(data_dir, monkeypatch):
    import core.credit_sentinel as cs
    sent = []

    async def _fake_deliver(container, user_id, text, **kw):
        sent.append(text)
        return "sent"

    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", _fake_deliver)
    asyncio.run(cs.trip_credit_sentinel("openrouter 402", container=object(), user_id="rob"))
    asyncio.run(cs.trip_credit_sentinel("openrouter 402", container=object(), user_id="rob"))
    assert len(sent) == 1, "already-active sentinel must not re-notify"
    assert "402" in sent[0]


def test_flag_off_disables_sentinel(data_dir, monkeypatch):
    monkeypatch.setenv("CREDIT_SENTINEL_ENABLED", "false")
    from core.credit_sentinel import credit_sentinel_active, trip_credit_sentinel
    asyncio.run(trip_credit_sentinel("402", container=None, user_id="rob"))
    assert credit_sentinel_active() is False


def test_dispatch_once_pauses_while_sentinel_active(data_dir):
    from core.credit_sentinel import trip_credit_sentinel
    from agents.task.goals.dispatcher import GoalDispatcher

    asyncio.run(trip_credit_sentinel("402", container=None, user_id="rob"))

    class _Board:
        def reclaim_stale(self):
            return 0

    class _Agent:
        pass

    os.environ["GOALS_ENABLED"] = "true"
    try:
        d = GoalDispatcher(_Board(), _Agent())
        assert asyncio.run(d.dispatch_once()) == 0
    finally:
        os.environ.pop("GOALS_ENABLED", None)


def test_cron_llm_tick_skipped_while_sentinel_active(data_dir):
    from core.credit_sentinel import trip_credit_sentinel
    from cron.runner import make_agent_runner
    from cron.jobs import CronJob

    asyncio.run(trip_credit_sentinel("402", container=None, user_id="rob"))

    created = []

    class _TA:
        async def create_session(self, *, user_id, request):
            created.append(1)
            return {"id": "s1"}

        async def run_session(self, user_id, session_id):
            return "Session completed successfully"

    os.environ["CRON_RUN_LOOP"] = "true"
    try:
        job = CronJob(id="j1", task="t", schedule_spec="30m", user_id="u1",
                      next_run_at=None, one_shot=True, skip_memory=True,
                      max_duration_seconds=60, payload={}, created_at=None)
        ok = asyncio.run(make_agent_runner(_TA())(job))
        assert ok is True, "a sentinel skip is a $0 tick, not a job failure"
        assert not created, "LLM tick must not run while the sentinel is active"
    finally:
        os.environ.pop("CRON_RUN_LOOP", None)


# NOTE (Task 10, 2026-07-16): `test_dispatcher_refusal_trips_sentinel_on_credit_death`
# used to live here, asserting that GoalDispatcher._run_goal itself tripped the
# sentinel when a fake agent's run_session() returned a 402-shaped string. That trip
# site was intentionally removed from goals/dispatcher.py (and cron/runner.py) and
# consolidated into ONE universal site: error_recovery.py::_handle_step_error, which
# is on every real LLM-error path (chat, goals, cron, sub-agents). A fake agent whose
# run_session() fabricates a status string bypasses the real Agent/ErrorRecoveryMixin,
# so it can no longer exercise a trip at the dispatcher layer.
#
# It was DELETED rather than rewritten — a mistake (caught in review 2026-07-16). The
# behaviour it guarded ("a credit-death failure latches the sentinel") still exists;
# it only moved. Dropping it left the end-to-end latch assertion with no owner, and
# the replacement tests all mocked trip_credit_sentinel out — so they passed against a
# trip site that was unreachable for the real prod 402 (it was nested under
# `is_permanent`, which the real error never satisfies). It is now rewritten against
# the current site as
# tests/unit/agents/task/agent/core/test_error_recovery_sentinel.py::
# test_integration_real_agent_credit_death_latches_real_sentinel — a real Agent driven
# through the real _handle_step_error into the real on-disk latch.
#
# The dispatcher's unchanged CHECK site is still covered by
# test_dispatch_once_pauses_while_sentinel_active above.


def test_sentinel_path_fallback_ignores_data_root(monkeypatch, tmp_path):
    """T3 (2026-07-16): the exception fallback must follow resolve_data_home
    precedence (POLYROB_DATA_DIR → cwd/.polyrob) — never the legacy
    "POLYROB_DATA_DIR or DATA_ROOT or 'data'" order. DATA_ROOT is the SESSION
    tree axis; latching the sentinel there means the safety gate reads a
    different file than the agent writes."""
    import core.credit_sentinel as cs

    def _boom():
        raise RuntimeError("bootstrap unavailable")

    monkeypatch.setattr("core.runtime_config.get_data_root", _boom)
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "sessions"))  # must NOT win
    monkeypatch.chdir(tmp_path)
    p = cs._sentinel_path()
    assert str(tmp_path / "sessions") not in p
    assert p == str(tmp_path / ".polyrob" / "CREDIT_SENTINEL")


def test_sentinel_path_fallback_honors_polyrob_data_dir(monkeypatch, tmp_path):
    import core.credit_sentinel as cs

    def _boom():
        raise RuntimeError("bootstrap unavailable")

    monkeypatch.setattr("core.runtime_config.get_data_root", _boom)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "sessions"))
    p = cs._sentinel_path()
    assert p == str(tmp_path / "home" / "CREDIT_SENTINEL")
