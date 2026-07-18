"""Owner daily digest — deterministic composition + delivery routing."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cron import digest
from cron.digest import compose_digest


def _split_ledger(**over):
    led = {
        "user_id": "rob", "window_days": 1,
        "treasury": {"income_usd": 0.0, "spend_usd": 0.0, "pending_usd": 2.0,
                     "pending_count": 1, "balance_usd": 10.0, "net_usd": 0.0,
                     "available": True},
        "runtime": {"spend_window_usd": 2.47, "spend_total_usd": 13.97,
                    "calls_window": 100, "calls_total": 561,
                    "provider_balance_usd": -0.17, "available": True},
        "costs_available": True, "inbound_available": True, "wallet_metering": "on",
    }
    led.update(over)
    return led


def _line_starting(text, prefix):
    """Return the single digest line starting with ``prefix``, or None."""
    for line in text.split("\n"):
        if line.strip().startswith(prefix):
            return line
    return None


@pytest.mark.asyncio
async def test_compose_digest_includes_money_and_activity(monkeypatch):
    monkeypatch.setattr(digest, "_ledger", lambda uid, days: _split_ledger(
        treasury={"income_usd": 2.0, "spend_usd": 0.5, "pending_usd": 3.0,
                  "pending_count": 1, "balance_usd": None, "net_usd": 1.5,
                  "available": True},
        runtime={"spend_window_usd": 0.1, "spend_total_usd": 0.4,
                 "calls_window": 1, "calls_total": 5,
                 "provider_balance_usd": None, "available": True}))
    monkeypatch.setattr(digest, "_event_aggregate", lambda uid, since: {
        "counts_by_kind": {"goal_run": 2, "self_modification": 1}, "total_events": 3})
    monkeypatch.setattr(digest, "_open_asks", lambda uid, data_dir: [{"title": "unblock me"}])
    monkeypatch.setattr(digest, "_episodes", lambda uid, since: [
        {"kind": "cron", "outcome": "done"}])
    text = await digest.compose_digest("u1", days=1)
    assert "Treasury: income $2.00, spend $0.50, net $1.50" in text
    assert "earned" not in text.lower()
    assert "1 pending invoice(s) $3.00" in text
    assert "2 goal event" in text and "1 self-change" in text
    assert "1 session" in text
    assert "unblock me" in text


@pytest.mark.asyncio
async def test_compose_digest_all_empty_is_stable(monkeypatch):
    """An unreadable ledger ({}) must render an honest 'no data' state, NOT
    fabricated $0.00 Treasury/Runtime lines (final review Finding 1 — H14b:
    the digest used to be byte-identical between a ledger read FAILURE and a
    genuinely quiet real-zero day; see
    test_ledger_unreadable_no_fabricated_money_lines /
    test_genuinely_quiet_day_renders_real_zeros below for the distinguishing
    pair)."""
    monkeypatch.setattr(digest, "_ledger", lambda uid, days: {})
    monkeypatch.setattr(digest, "_event_aggregate", lambda uid, since: {})
    monkeypatch.setattr(digest, "_open_asks", lambda uid, data_dir: [])
    monkeypatch.setattr(digest, "_episodes", lambda uid, since: [])
    text = await digest.compose_digest("u1", days=1)
    assert "Daily digest — today" in text
    assert "Treasury:" not in text
    assert "Runtime cost:" not in text
    assert "$0.00" not in text
    assert "no data" in text.lower()
    assert "earned" not in text.lower()
    assert "Pending approvals: none" in text


@pytest.mark.asyncio
async def test_ledger_unreadable_no_fabricated_money_lines(monkeypatch):
    """THE regression test for Finding 1: ``_ledger`` fails open to ``{}`` when
    the unified-ledger read raises. That must render an honest "no data"
    line instead of Treasury/Runtime $0.00 lines the owner cannot tell apart
    from a genuinely quiet day (see the paired test below — same digest,
    real zeros, and the money lines ARE rendered there)."""
    monkeypatch.setattr(digest, "_ledger", lambda uid, days: {})
    monkeypatch.setattr(digest, "_event_aggregate", lambda uid, since: {
        "counts_by_kind": {"goal_run": 1}})
    monkeypatch.setattr(digest, "_open_asks", lambda uid, data_dir: [])
    monkeypatch.setattr(digest, "_episodes", lambda uid, since: [
        {"kind": "cron", "outcome": "done"}])
    text = await digest.compose_digest("u1", days=1)
    assert "Treasury:" not in text
    assert "Runtime cost:" not in text
    assert "$0.00" not in text
    assert "no data" in text.lower() and "ledger" in text.lower()
    # Non-money sections are unaffected by an unreadable ledger.
    assert "1 goal event" in text


@pytest.mark.asyncio
async def test_genuinely_quiet_day_renders_real_zeros(monkeypatch):
    """The distinguishing half of the Finding 1 regression pair: a REAL ledger
    read that came back all-zero (every leg ``available: True``) is a
    genuinely quiet day, not a broken read — it DOES render $0.00 lines. The
    point of H14b is distinguishability, not that zero must never render."""
    monkeypatch.setattr(digest, "_ledger", lambda uid, days: _split_ledger(
        treasury={"income_usd": 0.0, "spend_usd": 0.0, "pending_usd": 0.0,
                  "pending_count": 0, "balance_usd": None, "net_usd": 0.0,
                  "available": True},
        runtime={"spend_window_usd": 0.0, "spend_total_usd": 0.0,
                 "calls_window": 0, "calls_total": 0,
                 "provider_balance_usd": None, "available": True}))
    monkeypatch.setattr(digest, "_event_aggregate", lambda uid, since: {})
    monkeypatch.setattr(digest, "_open_asks", lambda uid, data_dir: [])
    monkeypatch.setattr(digest, "_episodes", lambda uid, since: [])
    text = await digest.compose_digest("u1", days=1)
    assert "Treasury: income $0.00, spend $0.00, net $0.00" in text
    assert "Runtime cost: $0.00 today" in text
    assert "no data" not in text.lower()


@pytest.mark.asyncio
async def test_degraded_ledger_appends_availability_note(monkeypatch):
    """A PARTIALLY degraded ledger (some legs read, some didn't — distinct
    from the fully-unreadable {} case) still renders the numbers it has, but
    must say so via ledger_availability_note (H14b) — mirrors core/recap.py
    and cli/ui/commands/h_finance.py, which already do this."""
    monkeypatch.setattr(digest, "_ledger", lambda uid, days: _split_ledger(
        costs_available=False))
    monkeypatch.setattr(digest, "_event_aggregate", lambda uid, since: {})
    monkeypatch.setattr(digest, "_open_asks", lambda uid, data_dir: [])
    monkeypatch.setattr(digest, "_episodes", lambda uid, since: [])
    text = await digest.compose_digest("u1", days=1)
    assert "⚠" in text
    assert "metering degraded" in text.lower()
    # The numbers this leg's fixture DOES have still render — degradation
    # annotates, it doesn't blank the whole section.
    assert "Runtime cost: $2.47 today" in text


@pytest.mark.asyncio
async def test_digest_never_merges_treasury_and_runtime(monkeypatch):
    """The 2026-07-16 bug: 'Money: earned $0.00, spent $2.47, net $-2.47' reported
    the owner's API bill as Rob's P&L while his wallet was untouched."""
    monkeypatch.setattr("cron.digest._ledger", lambda u, d: _split_ledger())
    monkeypatch.setattr("cron.digest._event_aggregate", lambda u, t: {"counts_by_kind": {}})
    monkeypatch.setattr("cron.digest._open_asks", lambda u, d: [])
    monkeypatch.setattr("cron.digest._episodes", lambda u, t: [])

    out = await compose_digest("rob", days=1)
    assert "Treasury: income $0.00, spend $0.00, net $0.00" in out
    assert "Runtime cost: $2.47 today" in out
    assert "$13.97 total" in out
    assert "net $-2.47" not in out      # the old merged lie


@pytest.mark.asyncio
async def test_runtime_balance_omitted_when_none(monkeypatch):
    """provider_balance_usd=None must NEVER render as a fabricated $0.00 —
    the balance is simply absent from the Runtime cost line."""
    monkeypatch.setattr(digest, "_ledger", lambda uid, days: _split_ledger(
        runtime={"spend_window_usd": 2.47, "spend_total_usd": 13.97,
                 "calls_window": 100, "calls_total": 561,
                 "provider_balance_usd": None, "available": True}))
    monkeypatch.setattr(digest, "_event_aggregate", lambda uid, since: {})
    monkeypatch.setattr(digest, "_open_asks", lambda uid, data_dir: [])
    monkeypatch.setattr(digest, "_episodes", lambda uid, since: [])
    text = await digest.compose_digest("u1", days=1)
    runtime_line = _line_starting(text, "• Runtime cost:")
    assert runtime_line is not None
    assert "balance" not in runtime_line


@pytest.mark.asyncio
async def test_runtime_balance_rendered_when_present(monkeypatch):
    monkeypatch.setattr(digest, "_ledger", lambda uid, days: _split_ledger())
    monkeypatch.setattr(digest, "_event_aggregate", lambda uid, since: {})
    monkeypatch.setattr(digest, "_open_asks", lambda uid, data_dir: [])
    monkeypatch.setattr(digest, "_episodes", lambda uid, since: [])
    text = await digest.compose_digest("u1", days=1)
    runtime_line = _line_starting(text, "• Runtime cost:")
    assert runtime_line is not None
    assert "balance $-0.17" in runtime_line


@pytest.mark.asyncio
async def test_treasury_balance_omitted_when_none(monkeypatch):
    """treasury.balance_usd=None must NEVER render as a fabricated $0.00 —
    the balance is simply absent from the Treasury line (independent of the
    Runtime cost line, which keeps its own balance in this fixture)."""
    monkeypatch.setattr(digest, "_ledger", lambda uid, days: _split_ledger(
        treasury={"income_usd": 0.0, "spend_usd": 0.0, "pending_usd": 2.0,
                  "pending_count": 1, "balance_usd": None, "net_usd": 0.0,
                  "available": True}))
    monkeypatch.setattr(digest, "_event_aggregate", lambda uid, since: {})
    monkeypatch.setattr(digest, "_open_asks", lambda uid, data_dir: [])
    monkeypatch.setattr(digest, "_episodes", lambda uid, since: [])
    text = await digest.compose_digest("u1", days=1)
    treasury_line = _line_starting(text, "• Treasury:")
    assert treasury_line is not None
    assert "balance" not in treasury_line


@pytest.mark.asyncio
async def test_treasury_balance_rendered_when_present(monkeypatch):
    """The agent's own wallet balance (treasury.balance_usd) must render on the
    Treasury line — the incident this project fixes was the wallet sitting at
    $10 untouched while the digest only reported the (unrelated) runtime bill."""
    monkeypatch.setattr(digest, "_ledger", lambda uid, days: _split_ledger())
    monkeypatch.setattr(digest, "_event_aggregate", lambda uid, since: {})
    monkeypatch.setattr(digest, "_open_asks", lambda uid, data_dir: [])
    monkeypatch.setattr(digest, "_episodes", lambda uid, since: [])
    text = await digest.compose_digest("u1", days=1)
    treasury_line = _line_starting(text, "• Treasury:")
    assert treasury_line is not None
    assert "balance $10.00" in treasury_line


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
    async def _fake_build(user_id, *, days=7, db=None, include_balances=False):
        return {"earned_usd": 9.0, "total_spend_usd": 2.0, "net_usd": 7.0}

    monkeypatch.setattr("modules.credits.unified_ledger.build_ledger", _fake_build)
    # we are already inside a running loop (pytest.mark.asyncio)
    result = digest._ledger("u1", 1)
    assert result.get("earned_usd") == 9.0
