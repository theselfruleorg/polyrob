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
    # conftest defaults the sentinel OFF for every test; this suite tests the
    # sentinel itself, so opt back in (individual tests may re-disable).
    monkeypatch.setenv("CREDIT_SENTINEL_ENABLED", "true")
    return tmp_path


def test_looks_like_credit_death_matches_provider_shapes():
    from core.credit_sentinel import looks_like_credit_death
    assert looks_like_credit_death(
        "Session failed: PERMANENT ERROR: Error code: 402 - insufficient credits") is True
    assert looks_like_credit_death("insufficient_quota: billing hard limit") is True
    assert looks_like_credit_death("Payment Required") is True
    assert looks_like_credit_death("Session failed: browser crashed") is False
    assert looks_like_credit_death(None) is False


def test_402_marker_requires_word_boundary():
    """Validation fix (2026-07-23): bare-substring "402" tripped on formatted
    amounts, token counts and hex request ids. Real provider shapes carry
    non-word neighbors around 402 and must keep matching — pinned to the
    captured prod payload class (Task 10 lesson: pin REAL shapes)."""
    from core.credit_sentinel import looks_like_credit_death
    # real provider shapes still match
    assert looks_like_credit_death(
        "Error code: 402 - This request requires more credits, or fewer "
        "max_tokens. You requested up to 8192 tokens, but can only afford 1591") is True
    assert looks_like_credit_death("HTTP/1.1 402 Payment Required") is True
    assert looks_like_credit_death("status_code=402") is True
    assert looks_like_credit_death("(402)") is True
    # digit-run / identifier coincidences must NOT match
    assert looks_like_credit_death(
        "run_budget_exhausted: session provider spend $0.4020 reached "
        "RUN_BUDGET_USD $0.40") is False
    assert looks_like_credit_death("requested 130402 tokens, max is 128000") is False
    assert looks_like_credit_death(
        "upstream call failed (request_id: req_9f402ab13c7e)") is False


def test_trip_activates_and_auto_releases(data_dir, monkeypatch):
    from core.credit_sentinel import (credit_sentinel_active, trip_credit_sentinel,
                                      _sentinel_path)
    assert credit_sentinel_active() is False
    asyncio.run(trip_credit_sentinel("openrouter 402", container=None, user_id="rob"))
    assert credit_sentinel_active() is True
    # age the latch past the release window → auto-release
    p = _sentinel_path()
    state = json.loads(open(p).read())
    stale = time.time() - 100 * 3600
    # Latch entries are per provider since 2026-08-14; age every one of them.
    for entry in state["providers"].values():
        entry["ts"] = stale
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


def test_retrip_notice_text_is_byte_distinct(data_dir, monkeypatch):
    """020 #1: a re-trip after auto-release must produce byte-distinct notice
    text (trip timestamp stamped in), or the rail's 24h content dedup silently
    eats every pause notice after the first."""
    import core.credit_sentinel as cs
    sent = []

    async def _fake_deliver(container, user_id, text, **kw):
        sent.append(text)
        return "sent"

    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", _fake_deliver)

    fake_now = [time.struct_time((2026, 7, 19, 8, 16, 0, 5, 200, 0))]
    monkeypatch.setattr(time, "gmtime", lambda *a: fake_now[0])

    asyncio.run(cs.trip_credit_sentinel("openrouter 402", container=object(), user_id="rob"))
    os.remove(cs._sentinel_path())  # simulate auto-release
    fake_now[0] = time.struct_time((2026, 7, 19, 14, 16, 0, 5, 200, 0))
    asyncio.run(cs.trip_credit_sentinel("openrouter 402", container=object(), user_id="rob"))

    assert len(sent) == 2
    assert sent[0] != sent[1], "re-trip notice must be byte-distinct from the first"
    assert "08:16" in sent[0] and "14:16" in sent[1]


def test_deduped_sentinel_notice_warns(data_dir, monkeypatch, caplog):
    """020 #3: if a sentinel pause notice is ever deduped anyway, WARN loudly."""
    import logging
    import core.credit_sentinel as cs

    async def _fake_deliver(container, user_id, text, **kw):
        return "deduped"

    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", _fake_deliver)
    with caplog.at_level(logging.WARNING, logger=cs.logger.name):
        asyncio.run(cs.trip_credit_sentinel("openrouter 402", container=object(), user_id="rob"))
    assert any("DEDUPED" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Provider scoping (2026-08-14). Live prod: Rob ran a HEALTHY flat-rate
# zai-coding seat, but one legacy cron job still pinned the credit-dead
# OpenRouter. Its 402 latched the GLOBAL sentinel and paused every goal —
# including ones correctly pinned to the working provider — for 5.7 hours.
# A dead provider must only pause work that would USE that provider.
# ---------------------------------------------------------------------------

def test_trip_is_scoped_to_the_failing_provider(data_dir):
    from core.credit_sentinel import credit_sentinel_active, trip_credit_sentinel
    asyncio.run(trip_credit_sentinel("Error code: 402 - insufficient credits",
                                     provider="openrouter"))
    # the dead provider is paused
    assert credit_sentinel_active("openrouter") is True
    # a different, healthy provider is NOT
    assert credit_sentinel_active("zai-coding") is False


def test_unscoped_query_still_sees_any_active_latch(data_dir):
    """Legacy callers pass no provider and must keep their old meaning."""
    from core.credit_sentinel import credit_sentinel_active, trip_credit_sentinel
    asyncio.run(trip_credit_sentinel("402 insufficient credits", provider="openrouter"))
    assert credit_sentinel_active() is True


def test_unscoped_trip_pauses_everything(data_dir):
    """An unattributable credit death keeps the conservative global behaviour."""
    from core.credit_sentinel import credit_sentinel_active, trip_credit_sentinel
    asyncio.run(trip_credit_sentinel("402 insufficient credits"))
    assert credit_sentinel_active() is True
    assert credit_sentinel_active("zai-coding") is True


def test_legacy_unkeyed_latch_file_is_read_as_global(data_dir):
    """A latch written by the pre-scoping build must not be silently ignored —
    that would un-pause a genuinely dead provider on upgrade."""
    from core.credit_sentinel import credit_sentinel_active
    (data_dir / "CREDIT_SENTINEL").write_text(
        json.dumps({"ts": time.time(), "reason": "402 legacy"}))
    assert credit_sentinel_active() is True
    assert credit_sentinel_active("zai-coding") is True


def test_two_providers_latch_independently(data_dir):
    from core.credit_sentinel import credit_sentinel_active, trip_credit_sentinel
    asyncio.run(trip_credit_sentinel("402", provider="openrouter"))
    asyncio.run(trip_credit_sentinel("402", provider="openai"))
    assert credit_sentinel_active("openrouter") is True
    assert credit_sentinel_active("openai") is True
    assert credit_sentinel_active("zai-coding") is False


def test_scoped_entry_auto_releases_on_its_own_clock(data_dir, monkeypatch):
    from core.credit_sentinel import credit_sentinel_active
    stale = time.time() - (7 * 3600)  # older than the 6h default window
    (data_dir / "CREDIT_SENTINEL").write_text(
        json.dumps({"providers": {"openrouter": {"ts": stale, "reason": "402"}}}))
    assert credit_sentinel_active("openrouter") is False
    assert credit_sentinel_active() is False


# ---------------------------------------------------------------------------
# Plan-quota exhaustion (z.ai 1310) + provider-stated reset time (2026-08-16)
# ---------------------------------------------------------------------------

def test_looks_like_credit_death_matches_plan_quota_exhaustion():
    from core.credit_sentinel import looks_like_credit_death
    assert looks_like_credit_death(
        "Error code: 429 - [1310][Weekly/Monthly Limit Exhausted. "
        "Your limit will reset at 2026-08-18 18:01:49]") is True
    assert looks_like_credit_death("Error code: 429 - insufficient balance") is True
    # transient rate limiting must NOT latch the sentinel
    assert looks_like_credit_death("429 too many requests, retry after 3s") is False


def test_extract_reset_ts(monkeypatch):
    import calendar
    import time as _time
    from core import credit_sentinel as cs

    as_utc = float(calendar.timegm((2026, 8, 18, 18, 1, 49, 0, 0, 0)))
    now = as_utc - 2 * 86400
    monkeypatch.setattr(_time, "time", lambda: now)
    text = ("[1310][Weekly/Monthly Limit Exhausted. Your limit will reset at "
            "2026-08-18 18:01:49][202608161600034d3278b09a014e95]")
    # earliest plausible reading wins (providers rarely state a timezone; z.ai
    # stamps UTC+8 — releasing early costs one probe, releasing late costs
    # hours of dead autonomy after recovery)
    assert cs.extract_reset_ts(text) == as_utc - 8 * 3600
    # already past -> None (callers fall back to the fixed window)
    monkeypatch.setattr(_time, "time", lambda: as_utc + 100)
    assert cs.extract_reset_ts(text) is None
    # no reset phrase -> None
    monkeypatch.setattr(_time, "time", lambda: now)
    assert cs.extract_reset_ts("429 too many requests") is None
    assert cs.extract_reset_ts(None) is None
    # a far-future parse is capped so a garbled timestamp can't pause a month
    far = "your quota will reset at 2026-09-30 00:00:00 thanks"
    assert cs.extract_reset_ts(far) == now + 7 * 86400


def test_release_ts_overrides_fixed_window(data_dir, monkeypatch):
    from core.credit_sentinel import credit_sentinel_active, trip_credit_sentinel
    # a 0h window would release instantly under the legacy clock…
    monkeypatch.setenv("CREDIT_SENTINEL_RELEASE_HOURS", "0")
    asyncio.run(trip_credit_sentinel(
        "[1310] Weekly/Monthly Limit Exhausted", provider="zai-coding",
        release_ts=time.time() + 3600))
    # …but the provider-stated reset holds the latch
    assert credit_sentinel_active("zai-coding") is True
    # scoping unchanged: other providers keep serving
    assert credit_sentinel_active("openrouter") is False


def test_release_ts_expiry_releases(data_dir):
    from core.credit_sentinel import credit_sentinel_active, trip_credit_sentinel
    asyncio.run(trip_credit_sentinel(
        "[1310] Weekly/Monthly Limit Exhausted", provider="zai-coding",
        release_ts=time.time() - 5))
    # the stated reset already passed -> released even though ts is fresh
    assert credit_sentinel_active("zai-coding") is False


def test_release_ts_survives_latch_roundtrip(data_dir):
    from core.credit_sentinel import (_read_latch, _sentinel_path,
                                      trip_credit_sentinel)
    release = time.time() + 7200
    asyncio.run(trip_credit_sentinel(
        "[1310] limit exhausted", provider="zai-coding", release_ts=release))
    entries = _read_latch(_sentinel_path())
    assert entries["zai-coding"]["release_ts"] == pytest.approx(release)
