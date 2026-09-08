"""Status snapshot SSOT (2026-08-28) — the regression test for "confident and wrong".

On 2026-08-28 the owner's /status rendered "Goals: 0 open, 0 running · kill
switch: clear" while OpenRouter was credit-dead, 91/92 owner notices had been
suppressed, two asks were open and a goal was blocked. This file builds that
exact degraded state as a fixture and asserts every condition is rendered —
and that a source we cannot read renders as `unavailable (<reason>)`, never as
a zero or a missing line.
"""
import asyncio
import json
import os
import time

import pytest

from core.status_snapshot import (
    OVERALL_DEGRADED, OVERALL_OK, OVERALL_PARTIAL, SECTION_ORDER, STATE_UNAVAILABLE,
    build_status_snapshot,
)
from core.status_render import (
    render_agent_health_note, render_health_lines, render_status_lines, render_status_text,
)

OWNER = "rob"


def _seed_goals(data_dir, *, ready=0, blocked=True, asks=True, exhausted_objective=True):
    from agents.task.goals.board import GoalBoard, STATUS_READY
    board = GoalBoard(os.path.join(data_dir, "goals.db"))
    for i in range(ready):
        board.create(user_id=OWNER, title=f"Ready goal {i}", status=STATUS_READY)
    if blocked:
        g = board.create(user_id=OWNER, title="Execute first treasury trade once defi_trade is granted")
        board.record_failure(g.id, error="agent declared BLOCKED: need the owner to grant defi_trade")
        board.update_status(g.id, "blocked")
    if asks:
        board.create_ask(user_id=OWNER, what="Unblock goal: deploy one x402 endpoint",
                         why="need a public HTTPS endpoint", blocks_goal_ids=[])
    if exhausted_objective:
        obj = board.create_objective(user_id=OWNER, title="Promote POLYROB in public",
                                     payload={"goal_budget": 2})
        for title in ("Write the launch post", "Ship the demo video"):
            c = board.create(user_id=OWNER, title=title, parent_id=obj.id, force=True)
            board.update_status(c.id, "done")  # a done child still counts against the budget
    # another tenant's ask must never leak into rob's snapshot
    board.create_ask(user_id="someone-else", what="Not rob's ask", why="", blocks_goal_ids=[])
    return board


def _seed_cron(data_dir):
    from datetime import datetime, timedelta, timezone
    from cron.jobs import CronJob, CronJobStore
    store = CronJobStore(os.path.join(data_dir, "cron.db"))
    now = datetime.now(timezone.utc)
    store.add(CronJob(id="job1", task="Daily digest", schedule_spec="every day 09:00",
                      user_id=OWNER, next_run_at=now + timedelta(hours=1), enabled=True,
                      created_at=now))
    store.add(CronJob(id="job2", task="Old job", schedule_spec="30m", user_id=OWNER,
                      next_run_at=now - timedelta(days=9), enabled=False, created_at=now))
    return store


def _seed_telemetry(path, *, capped=3, timeouts=1, rerouted=2):
    from core.event_log import TelemetryEventLog
    log = TelemetryEventLog(path)
    now = time.time()
    log.record("credit_sentinel", user_id=OWNER, source="credit_sentinel",
               attrs={"reason": "402 insufficient credits"}, ts=now - 600)
    for i in range(capped):
        log.record("user_delivery", user_id=OWNER, source="self_evolution",
                   attrs={"outcome": "capped", "content_hash": f"h{i}",
                          "text": f"goal started {i}"}, ts=now - 100 - i)
        log.record("owner_notice", user_id=OWNER, source="user_delivery",
                   attrs={"text": f"[suppressed by daily proactive-message cap; "
                                  f"source=self_evolution] goal started {i}"}, ts=now - 100 - i)
    for i in range(4):
        log.record("user_delivery", user_id=OWNER, source="agent_send",
                   attrs={"outcome": "sent", "content_hash": f"s{i}"}, ts=now - 50 - i)
    for i in range(timeouts):
        log.record("tool_timeout", user_id=OWNER, source="controller",
                   attrs={"tool": "browser"}, ts=now - 30)
    for i in range(rerouted):
        log.record("cron_run", user_id=OWNER, source="cron",
                   attrs={"job_id": "job1", "outcome": "provider_rerouted", "reason": "zai"},
                   ts=now - 20 - i)
        log.record("cron_run", user_id=OWNER, source="cron",
                   attrs={"job_id": "job1", "outcome": "done"}, ts=now - 19 - i)
    log.record("goal_run", user_id=OWNER, source="goals",
               attrs={"goal_id": "g1", "outcome": "done"}, ts=now - 10)
    log.record("self_wake", user_id=OWNER, source="self_wake", attrs={"outcome": "fired"},
               ts=now - 5)
    return log


@pytest.fixture()
def degraded(tmp_path, monkeypatch):
    """The 2026-08-28 prod state: sentinel tripped + suppressed notices + open
    ask + blocked goal + 0 ready goals + exhausted objective + rerouted cron."""
    data_dir = str(tmp_path)
    monkeypatch.setenv("POLYROB_DATA_DIR", data_dir)
    monkeypatch.setenv("CREDIT_SENTINEL_ENABLED", "true")
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", os.path.join(data_dir, "telemetry_events.db"))
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("CRON_ENABLED", "true")
    monkeypatch.setenv("DEFAULT_PROVIDER", "openrouter")
    with open(os.path.join(data_dir, "CREDIT_SENTINEL"), "w") as f:
        json.dump({"providers": {"openrouter": {
            "ts": time.time() - 3600, "release_ts": None,
            "reason": "from OpenRouter: Error code: 402 - Insufficient credits"}}}, f)
    _seed_goals(data_dir)
    _seed_cron(data_dir)
    _seed_telemetry(os.path.join(data_dir, "telemetry_events.db"))
    return data_dir


def _fake_ledger():
    return {
        "user_id": OWNER, "window_days": 1,
        "treasury": {"income_usd": 1.0, "spend_usd": 0.5, "net_usd": 0.5, "pending_usd": 0.0,
                     "pending_count": 0, "balance_usd": None, "available": True},
        "runtime": {"spend_window_usd": 2.0, "spend_total_usd": 40.0, "calls_window": 5,
                    "calls_total": 100, "provider_balance_usd": None, "available": True},
        "costs_available": True, "inbound_available": True, "wallet_metering": "on",
    }


# --- the regression test ------------------------------------------------------

def test_degraded_fixture_renders_every_condition(degraded):
    snap = build_status_snapshot(OWNER, data_dir=degraded, ledger=_fake_ledger())
    assert snap.overall == OVERALL_DEGRADED
    keys = {h.key for h in snap.health}
    assert "credit_sentinel:openrouter" in keys
    assert any(k.startswith("ask:") for k in keys)
    assert any(k.startswith("blocked:") for k in keys)
    assert any(k.startswith("objective_budget:") for k in keys)
    assert "delivery_capped" in keys
    assert "tool_timeouts" in keys
    assert "loop_heartbeat:cron" in keys and "loop_heartbeat:goals" in keys
    text = render_status_text(snap)
    # health first, ranked: the critical sentinel line precedes every warning
    lines = text.splitlines()
    # 031: the pause line leads every seat (running here), THEN health, ranked
    assert lines[1].startswith("▶ RUNNING") or lines[1].startswith("⏸ PAUSED")
    assert lines[2].startswith("Health: DEGRADED")
    assert "credit sentinel TRIPPED for openrouter" in lines[3]
    assert "402" in lines[3]
    assert "open ask" in text and "x402 endpoint" in text and "/fulfill" in text
    assert "goal BLOCKED" in text and "defi_trade" in text
    assert "objective at lifetime goal budget (2/2)" in text
    assert "3 owner message(s) suppressed by the daily cap" in text and "/missed" in text
    assert "1 tool timeout(s)" in text
    assert "no liveness heartbeat" in text
    assert "0 ready, 0 running, 1 blocked" in text
    assert "2 cron run(s) rerouted off the pin" in text
    assert "cron: 1 enabled / 1 disabled" in text
    # tenant scoping: the other tenant's ask is invisible
    assert "Not rob's ask" not in text
    # money is labelled, never merged
    assert "treasury cash flow (income − spend; open positions NOT included): net $+0.50" in text
    assert "runtime cost (owner's compute bill): $2.00 last 24h · $40.00 total" in text


def test_every_section_is_always_present(degraded):
    snap = build_status_snapshot(OWNER, data_dir=degraded, ledger=_fake_ledger())
    assert tuple(snap.sections) == SECTION_ORDER
    rendered = "\n".join(render_status_lines(snap))
    for title in ("Session:", "Providers:", "Goals:", "Approvals:", "Loops:",
                  "Delivery:", "Posture:", "Money:"):
        assert title in rendered, title


def test_unreadable_source_renders_unavailable_never_zero(degraded):
    """Acceptance #4: kill a data source and the section says so."""
    os.rename(os.path.join(degraded, "goals.db"), os.path.join(degraded, "goals.db.bak"))
    snap = build_status_snapshot(OWNER, data_dir=degraded, ledger=_fake_ledger())
    work = snap.section("work")
    assert work.state == STATE_UNAVAILABLE
    assert "goals.db not found" in work.reason
    text = render_status_text(snap)
    assert "Goals: unavailable (FileNotFoundError: goals.db not found" in text
    assert "0 ready" not in text
    # still degraded (sentinel etc.) AND the unreadable source is named up top
    assert snap.overall == OVERALL_DEGRADED
    assert "could not verify: work (" in text
    # the missing store was NOT created as a side effect of reading status
    assert not os.path.exists(os.path.join(degraded, "goals.db"))


def test_money_failure_is_reported_not_dropped(degraded):
    snap = build_status_snapshot(OWNER, data_dir=degraded,
                                 ledger=RuntimeError("ledger boom"))
    assert snap.section("money").state == STATE_UNAVAILABLE
    text = render_status_text(snap)
    assert "Money: unavailable (RuntimeError: ledger boom)" in text
    assert "$0.00" not in text


def test_corrupt_telemetry_makes_delivery_unavailable(degraded, monkeypatch):
    path = os.path.join(degraded, "telemetry_events.db")
    with open(path, "wb") as f:
        f.write(b"not a database")
    snap = build_status_snapshot(OWNER, data_dir=degraded, ledger=_fake_ledger())
    assert snap.section("delivery").state == STATE_UNAVAILABLE
    text = render_status_text(snap)
    assert "Delivery: unavailable (" in text
    assert "0 suppressed" not in text


def test_clean_fixture_is_ok_with_evidence(tmp_path, monkeypatch):
    """OK is a claim with evidence: the headline lists what was checked."""
    data_dir = str(tmp_path)
    monkeypatch.setenv("POLYROB_DATA_DIR", data_dir)
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", os.path.join(data_dir, "telemetry_events.db"))
    monkeypatch.setenv("GOALS_ENABLED", "false")
    monkeypatch.setenv("CRON_ENABLED", "false")
    _seed_goals(data_dir, ready=1, blocked=False, asks=False, exhausted_objective=False)
    _seed_cron(data_dir)
    from core.event_log import TelemetryEventLog
    TelemetryEventLog(os.path.join(data_dir, "telemetry_events.db"))
    snap = build_status_snapshot(OWNER, data_dir=data_dir, ledger=_fake_ledger())
    assert snap.overall == OVERALL_OK, [h.text for h in snap.health] + snap.unavailable_sources
    head = render_health_lines(snap)[0]
    assert head.startswith("Health: OK") and "checked:" in head and "credit sentinel" in head


def test_partial_when_a_health_source_is_unreadable(tmp_path, monkeypatch):
    """No issue found but a source could not be read ⇒ PARTIAL, never OK."""
    data_dir = str(tmp_path)
    monkeypatch.setenv("POLYROB_DATA_DIR", data_dir)
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", os.path.join(data_dir, "telemetry_events.db"))
    monkeypatch.setenv("GOALS_ENABLED", "false")
    monkeypatch.setenv("CRON_ENABLED", "false")
    _seed_goals(data_dir, blocked=False, asks=False, exhausted_objective=False)
    # no cron.db, no telemetry db
    snap = build_status_snapshot(OWNER, data_dir=data_dir, ledger=_fake_ledger())
    assert snap.overall == OVERALL_PARTIAL
    assert render_health_lines(snap)[0].startswith("Health: PARTIAL")


def test_agent_note_carries_the_same_health_as_the_owner_view(degraded):
    snap = build_status_snapshot(OWNER, data_dir=degraded, include_money=False)
    note = render_agent_health_note(snap)
    assert "Health: DEGRADED" in note
    assert "credit sentinel TRIPPED for openrouter" in note
    assert "suppressed by the daily cap" in note
    assert "do not claim a clean state" in note


def test_session_liveness_uses_the_execution_lock(degraded):
    class _MM:
        def get_context_usage_percent(self):
            return 42.0

    class _St:
        n_steps = 7

    class _Inner:
        model_name = "grok-4.3"
        message_manager = _MM()
        state = _St()

    class _Orch:
        agents = {"main": _Inner()}

    class _TA:
        _session_execution_locks = {"s1": asyncio.Lock()}

        def get_orchestrator(self, sid):
            return _Orch() if sid == "s1" else None

        def _session_has_pending_input(self, sid):
            return False

    ta = _TA()
    idle = build_status_snapshot(OWNER, data_dir=degraded, task_agent=ta, session_id="s1",
                                 include_money=False)
    assert "idle" in idle.section("session").lines[0]
    ta._session_execution_locks["s1"]._locked = True
    busy = build_status_snapshot(OWNER, data_dir=degraded, task_agent=ta, session_id="s1",
                                 include_money=False)
    assert "running (step 7)" in busy.section("session").lines[0]
    assert "model=grok-4.3" in busy.section("session").lines[0]

    class _NoLock(_TA):
        _session_execution_locks = None

    unknown = build_status_snapshot(OWNER, data_dir=degraded, task_agent=_NoLock(),
                                    session_id="s1", include_money=False)
    assert "state unknown" in unknown.section("session").lines[0]


def test_empty_tenant_is_refused_not_widened(degraded):
    snap = build_status_snapshot("", data_dir=degraded, include_money=False)
    for name in ("work", "approvals", "loops", "delivery"):
        assert snap.section(name).state == STATE_UNAVAILABLE
        assert "no tenant" in snap.section(name).reason


# --- literal parity with the owning modules ----------------------------------

def test_literals_match_owning_modules():
    from agents.task.goals import board as b
    from core import event_kinds as ek
    from core import status_snapshot as ss
    from core.surfaces import user_delivery as ud
    from tools.controller.approval_queue import TOOL_APPROVAL_ASK_KIND
    assert (ss._KIND_GOAL, ss._KIND_OBJECTIVE, ss._KIND_ASK) == (b.KIND_GOAL, b.KIND_OBJECTIVE, b.KIND_ASK)
    assert (ss._ST_READY, ss._ST_RUNNING, ss._ST_BLOCKED, ss._ST_WAITING) == (
        b.STATUS_READY, b.STATUS_RUNNING, b.STATUS_BLOCKED, b.STATUS_WAITING)
    assert ss._ASK_OPEN == b.ASK_OPEN and ss._OBJ_ACTIVE == b.OBJ_ACTIVE
    assert ss._TOOL_APPROVAL_ASK_KIND == TOOL_APPROVAL_ASK_KIND
    assert (ss._K_USER_DELIVERY, ss._K_OWNER_NOTICE, ss._K_CREDIT_SENTINEL, ss._K_CRON_RUN,
            ss._K_GOAL_RUN, ss._K_SELF_WAKE, ss._K_TOOL_TIMEOUT, ss._K_TOOL_DENIED,
            ss._K_RUN_DEGRADED, ss._K_AUTONOMY_TICK) == (
        ek.USER_DELIVERY, ek.OWNER_NOTICE, ek.CREDIT_SENTINEL, ek.CRON_RUN, ek.GOAL_RUN,
        ek.SELF_WAKE, ek.TOOL_TIMEOUT, ek.TOOL_DENIED, ek.RUN_OUTCOME_DEGRADED, "autonomy_tick")
    assert (ss._K_SOCIAL_WRITE, ss._K_WALLET_SPEND) == (ek.SOCIAL_WRITE, ek.WALLET_SPEND)
    # the rail's suppression marker (user_delivery.py) is what /missed and the
    # delivery section grep for
    import inspect
    assert ss._SUPPRESSED_PREFIX in inspect.getsource(ud)


def test_objective_budget_mirrors_board_rule(monkeypatch):
    from core.status_snapshot import _objective_budget
    monkeypatch.setenv("OBJECTIVE_GOAL_BUDGET", "25")
    assert _objective_budget({}) == 25
    assert _objective_budget({"goal_budget": 12}) == 12
    assert _objective_budget({"stream_id": "treasury-trading", "goal_budget": 12}) == 0
