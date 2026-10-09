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
        board.record_failure(g.id, error="agent declared BLOCKED: need the owner to grant defi_trade",
            claim_token=board.get(g.id).claim_token)
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
    assert "loop_silent:cron" in keys and "loop_silent:goals" in keys
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


def test_dead_non_serving_provider_does_not_claim_to_block_live_provider(
        tmp_path, monkeypatch):
    data_dir = str(tmp_path)
    monkeypatch.setenv("POLYROB_DATA_DIR", data_dir)
    monkeypatch.setenv("CREDIT_SENTINEL_ENABLED", "true")
    monkeypatch.setenv("DEFAULT_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    with open(os.path.join(data_dir, "CREDIT_SENTINEL"), "w") as f:
        json.dump({"providers": {"zai-coding": {
            "ts": time.time() - 3600,
            "release_ts": time.time() + 3600,
            "reason": "weekly limit exhausted",
        }}}, f)

    snap = build_status_snapshot(OWNER, data_dir=data_dir, include_money=False)
    item = next(h for h in snap.health if h.key == "credit_sentinel:zai-coding")
    assert item.severity == "warn"
    assert "does NOT block live provider openrouter" in item.text


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


def test_dead_started_loop_is_a_health_item(tmp_path):
    """043 A8/A42 — the runtime's `autonomy_started` record names every loop it
    actually started (not just cron/goals); a started loop with no heartbeat
    at all is silent and must be flagged."""
    from core.event_log import TelemetryEventLog
    from core.status_snapshot import build_status_snapshot
    log = TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    log.record("autonomy_started", user_id="", source="runtime", attrs={"loops": ["cron", "settlement"]})
    log.record("autonomy_tick", user_id="", source="runtime", attrs={"loop": "cron", "alive": True})
    snap = build_status_snapshot("u1", data_dir=str(tmp_path), include_money=False)
    keys = {h.key for h in snap.health}
    assert "loop_silent:settlement" in keys


def test_live_started_loop_has_no_silent_item(tmp_path):
    """The companion to the dead-loop test above: a started loop with a fresh
    alive heartbeat must never render as silent."""
    from core.event_log import TelemetryEventLog
    from core.status_snapshot import build_status_snapshot
    log = TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    log.record("autonomy_started", user_id="", source="runtime", attrs={"loops": ["cron"]})
    log.record("autonomy_tick", user_id="", source="runtime", attrs={"loop": "cron", "alive": True})
    snap = build_status_snapshot("u1", data_dir=str(tmp_path), include_money=False)
    keys = {h.key for h in snap.health}
    assert not any(k.startswith("loop_silent:") for k in keys)


def test_old_started_row_is_still_read_past_the_24h_telemetry_window(tmp_path):
    """043 A8/A42 fix round 1 (Critical) — `autonomy_started` is written ONCE
    per process start, so after 24h of uptime (prod's normal state) it falls
    out of the windowed `tele` read the OTHER loop-section queries use. The
    expected-loop set must come from an UNBOUNDED read, or the whole feature
    silently decays back to cron/goals with no signal that anything narrowed."""
    from core.event_log import TelemetryEventLog
    from core.status_snapshot import build_status_snapshot
    now = time.time()
    log = TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    log.record("autonomy_started", user_id="", source="runtime",
               attrs={"loops": ["cron", "settlement"]}, ts=now - 3 * 86400)
    log.record("autonomy_tick", user_id="", source="runtime",
               attrs={"loop": "cron", "alive": True}, ts=now)
    snap = build_status_snapshot("u1", data_dir=str(tmp_path), include_money=False)
    keys = {h.key for h in snap.health}
    assert "loop_silent:settlement" in keys


def test_unreadable_autonomy_started_falls_back_and_warns(tmp_path, monkeypatch):
    """An unreadable autonomy_started read must never silently narrow the
    liveness set — it falls back to the gate-derived cron/goals pair AND
    raises a `loops_expected_unreadable` health item naming the narrowing."""
    import core.status_snapshot as ss
    from core.event_log import TelemetryEventLog
    monkeypatch.setenv("CRON_ENABLED", "true")
    monkeypatch.setenv("GOALS_ENABLED", "true")
    log = TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    log.record("autonomy_tick", user_id="", source="runtime", attrs={"loop": "cron", "alive": True})
    log.record("autonomy_tick", user_id="", source="runtime", attrs={"loop": "goals", "alive": True})

    orig_rows = ss._rows

    def _boom(db_path, sql, params=()):
        if ss._K_AUTONOMY_STARTED in params:
            raise RuntimeError("simulated read failure")
        return orig_rows(db_path, sql, params)

    monkeypatch.setattr(ss, "_rows", _boom)
    snap = ss.build_status_snapshot("u1", data_dir=str(tmp_path), include_money=False)
    keys = {h.key for h in snap.health}
    assert "loops_expected_unreadable" in keys
    # the fallback still ran: cron/goals are both fresh-alive, so no dead-loop
    # item, only the "we could not check the full set" warning.
    assert not any(k.startswith("loop_silent:") for k in keys)


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
    assert ss._K_WALLET_SPEND == ek.WALLET_SPEND
    assert ss._K_AUTONOMY_STARTED == ek.AUTONOMY_STARTED
    assert ss._K_SURFACE_POLL_ERROR == ek.SURFACE_POLL_ERROR
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


# --- spend record carries the chain (043 A36) --------------------------------

def test_section_titles_cover_every_section():
    """A section printed under its raw dict key (e.g. `creations`/`wallet`
    instead of `Made`/`Wallet`) on every status seat is the symptom of a
    section joining SECTION_ORDER without a matching title."""
    from core.status_render import _SECTION_TITLES
    assert set(SECTION_ORDER) <= set(_SECTION_TITLES)


def test_creations_row_renders_its_chain(tmp_path):
    from core.event_log import TelemetryEventLog
    from core.status_snapshot import _creations_section

    data_dir = str(tmp_path)
    log = TelemetryEventLog(os.path.join(data_dir, "telemetry_events.db"))
    log.record("wallet_spend", user_id=OWNER, source="wallet",
               attrs={"venue": "defi", "action": "launchpad_launch",
                      "counterparty": "0xTOKEN", "amount_usd": 1.0,
                      "result_ref": "0xtx", "chain": "robinhood"})
    sec = _creations_section(OWNER, data_dir)
    assert sec.data["creations"][0]["chain"] == "robinhood"
    assert any("on robinhood" in ln for ln in sec.lines)


def test_creations_row_carries_its_explorer_link(tmp_path):
    """043 A38/A5 — a creation on a chain with a pinned explorer renders a
    link, so the owner can verify the token without leaving the status seat."""
    from core.event_log import TelemetryEventLog
    from core.status_snapshot import _creations_section

    data_dir = str(tmp_path)
    log = TelemetryEventLog(os.path.join(data_dir, "telemetry_events.db"))
    log.record("wallet_spend", user_id=OWNER, source="wallet",
               attrs={"venue": "defi", "action": "deploy_token",
                      "counterparty": "0xTOKEN", "amount_usd": 1.0,
                      "result_ref": "0xtx", "chain": "base"})
    sec = _creations_section(OWNER, data_dir)
    assert sec.data["creations"][0]["url"] == "https://basescan.org/token/0xTOKEN"
    assert any("https://basescan.org/token/" in ln for ln in sec.lines)


# --- browser rail line (2026-09-17) -------------------------------------------

def test_identity_reports_the_browser_rail(degraded, monkeypatch):
    """The ONE seat every status surface renders says what the browser rail IS —
    never silent while the agent misdiagnoses it."""
    from core.security import browser_rail as br
    monkeypatch.setattr(br, "browser_rail_status",
                        lambda **k: br.BrowserRailStatus("unreachable", "cdp", True,
                                                         reason="connection refused"))
    snap = build_status_snapshot(OWNER, data_dir=degraded, ledger=_fake_ledger())
    ident = snap.section("identity")
    assert ident.data["browser_rail"] == "unreachable"
    line = next(l for l in ident.lines if l.startswith("browser:"))
    assert "configured, unreachable (connection refused)" in line
    item = next(h for h in snap.health if h.key == "browser_rail_unreachable")
    assert item.severity == "warn"


def test_unset_browser_rail_is_a_fact_not_a_health_item(degraded, monkeypatch):
    from core.security import browser_rail as br
    monkeypatch.setattr(br, "browser_rail_status",
                        lambda **k: br.BrowserRailStatus("unset", "local", True))
    snap = build_status_snapshot(OWNER, data_dir=degraded, ledger=_fake_ledger())
    ident = snap.section("identity")
    assert any("browser: none (custody)" in l for l in ident.lines)
    assert not any(h.key == "browser_rail_unreachable" for h in snap.health)


def test_snapshot_never_probes_a_remote_browser(degraded, monkeypatch):
    """No network read by default: the snapshot asks with probe=False."""
    from core.security import browser_rail as br
    seen = {}

    def fake(**k):
        seen.update(k)
        return br.BrowserRailStatus("configured", "wss", True)
    monkeypatch.setattr(br, "browser_rail_status", fake)
    build_status_snapshot(OWNER, data_dir=degraded, ledger=_fake_ledger())
    assert seen.get("probe") is False


def test_a_refused_money_rail_gate_is_a_crit_health_item(tmp_path):
    """2026-09-21: `defi_data.reconcile` — the money rails' step-1 gate — refused
    every call for 9 h (ledger > 1 MB) and every seat read healthy while six
    rails traded without it. The refusal is a `rail_precondition_failed` event;
    the snapshot must lead with it as CRIT, with the count, the first time and
    the reason, so a gate-down rail can never read healthy."""
    from core.event_log import TelemetryEventLog
    from core.status_snapshot import build_status_snapshot
    log = TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    for _ in range(3):
        log.record("rail_precondition_failed", user_id="u1", source="defi_data",
                   attrs={"tool": "reconcile", "chain": "robinhood",
                          "reason": "that file exceeds 1MB — not a position ledger"})
    snap = build_status_snapshot("u1", data_dir=str(tmp_path), include_money=False)
    items = {h.key: h for h in snap.health}
    assert "rail_gate_refused" in items
    h = items["rail_gate_refused"]
    assert h.severity == "crit"
    assert "reconcile" in h.text and "3" in h.text and "exceeds 1MB" in h.text
    assert h.remedy


def test_no_refused_gate_means_no_item(tmp_path):
    from core.event_log import TelemetryEventLog
    from core.status_snapshot import build_status_snapshot
    TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    snap = build_status_snapshot("u1", data_dir=str(tmp_path), include_money=False)
    assert "rail_gate_refused" not in {h.key for h in snap.health}


def _log_delivery(log, *, session_id, source, text, age_s, outcome="sent"):
    log.record("user_delivery", user_id="u1", session_id=session_id, source=source,
               attrs={"outcome": outcome, "text": text, "lane": "normal"},
               ts=time.time() - age_s)


def test_recent_notices_are_the_other_sessions_sends_in_the_last_minutes(tmp_path):
    """2026-09-21 15:33Z: the owner's "haven't i told you not to report about
    bugs?" landed 28 s after an AUTONOMOUS run's delivery notice, and the chat
    session — which never saw that notice — matched the correction to its own
    10:40 message. The delivery section now carries the LAST FEW MINUTES of
    sends by OTHER sessions (`recent_notices`), and the per-turn agent note
    renders them as "the owner was just told …", so a correction lands on the act."""
    from core.event_log import TelemetryEventLog
    from core.status_snapshot import build_status_snapshot
    log = TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    _log_delivery(log, session_id="chat-1", source="agent_send", text="my own reply", age_s=20)
    _log_delivery(log, session_id="cron-9", source="agent_send",
                  text="posted thread 2102058280738553979: my own ledger caught me", age_s=40)
    # 2026-09-22 06:07Z: "Explin better" landed 210 s after the EXIT rail's R6
    # escalation — a real owner reads first, then replies. The window is ten
    # minutes; a 20-minute-old send is still out.
    _log_delivery(log, session_id="cron-6", source="agent_send",
                  text="Exit rail ran. No trade — a real problem in the rules.", age_s=210)
    _log_delivery(log, session_id="cron-8", source="cron", text="old news", age_s=1200)
    _log_delivery(log, session_id="cron-7", source="agent_send", text="held", age_s=30,
                  outcome="quiet_held")
    snap = build_status_snapshot("u1", data_dir=str(tmp_path), session_id="chat-1",
                                 include_money=False)
    recent = snap.sections["delivery"].data["recent_notices"]
    assert [r["session_id"] for r in recent] == ["cron-9", "cron-6"]
    assert recent[0]["source"] == "agent_send"
    assert recent[0]["text"].startswith("posted thread")
    note = render_agent_health_note(snap)
    assert "the owner was just told" in note.lower()
    assert "you were just told" not in note.lower()
    assert "my own ledger caught me" in note
    assert "my own reply" not in note


def test_no_session_means_no_recent_notices_line(tmp_path):
    from core.event_log import TelemetryEventLog
    from core.status_snapshot import build_status_snapshot
    log = TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    _log_delivery(log, session_id="cron-9", source="agent_send", text="x", age_s=10)
    snap = build_status_snapshot("u1", data_dir=str(tmp_path), include_money=False)
    assert snap.sections["delivery"].data.get("recent_notices") == []
    assert "was just told" not in render_agent_health_note(snap).lower()


# -- the missed-messages TEXT, not just a count -------------------------------
#
# 2026-09-22: the delivery rail records every suppressed owner message as an
# `owner_notice` on the promise that it is "rolled into the digest". Prod's
# digest is the LLM cron job, which reads THIS snapshot — and the snapshot
# carried only `suppressed_notices`, a number. So the roll-up could report "16
# suppressed, all self_evolution" and never the one report the owner actually
# needed. The bodies live in `core.surfaces.missed.missed_notices`, the same ONE
# query `/missed` uses on every seat; the snapshot now carries a bounded copy so
# the digest can QUOTE them without sending a second daily message (the owner
# asked for "daily, text only" on 2026-09-20).

def test_delivery_carries_the_missed_bodies_not_only_the_count(degraded):
    snap = build_status_snapshot(OWNER, data_dir=degraded)
    data = snap.section("delivery").data
    assert data["suppressed_notices"] == 3, "the count stays"
    missed = data["missed"]
    assert len(missed) == 3, "and now the bodies are there too"
    for entry in missed:
        assert entry["kind"] == "capped"
        assert entry["text"].startswith("goal started"), \
            "the rail's marker prefix is stripped, as /missed strips it"
        assert isinstance(entry["ts"], float)
    assert data["missed_more"] == 0
    assert data.get("missed_unavailable") is None


def test_missed_is_bounded_and_says_how_many_it_left_out(tmp_path, monkeypatch):
    """A digest that quotes 200 bodies is not a digest. The cap is explicit and
    the remainder is COUNTED, never silently dropped."""
    from core.status_snapshot import MISSED_IN_SNAPSHOT
    data_dir = str(tmp_path)
    monkeypatch.setenv("POLYROB_DATA_DIR", data_dir)
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH",
                       os.path.join(data_dir, "telemetry_events.db"))
    extra = 4
    _seed_telemetry(os.path.join(data_dir, "telemetry_events.db"),
                    capped=MISSED_IN_SNAPSHOT + extra)
    snap = build_status_snapshot(OWNER, data_dir=data_dir)
    data = snap.section("delivery").data
    assert len(data["missed"]) == MISSED_IN_SNAPSHOT
    assert data["missed_more"] == extra
    assert data["suppressed_notices"] == MISSED_IN_SNAPSHOT + extra


def test_an_unreadable_notice_store_is_NAMED_never_rendered_as_none_missed(
        degraded, monkeypatch):
    """'I could not look' is a different fact from 'you missed nothing', and the
    second one told to a digest is a confident lie."""
    import core.surfaces.missed as missed_mod

    def _boom(*a, **kw):
        raise OSError("telemetry log not found")

    monkeypatch.setattr(missed_mod, "missed_notices", _boom)
    snap = build_status_snapshot(OWNER, data_dir=degraded)
    data = snap.section("delivery").data
    assert data["missed"] == []
    assert "OSError" in str(data["missed_unavailable"])
    assert data["suppressed_notices"] == 3, \
        "the independent count is unaffected by the body read failing"


def test_the_missed_bodies_are_RENDERED_not_just_stored(degraded):
    """The trap this item was filed about is "built but not wired": prod's
    digest is the LLM cron job and it reads the snapshot's LINES via
    `agent_status`, never `section.data`. A `data["missed"]` nobody renders
    would change nothing for the owner."""
    from core.status_render import render_section_lines
    snap = build_status_snapshot(OWNER, data_dir=degraded)
    lines = "\n".join(render_section_lines(snap, "delivery"))
    assert "missed" in lines.lower()
    assert "goal started" in lines, "the body itself must be quotable from the lines"


def test_an_unreadable_notice_store_says_so_in_the_LINES_too(degraded, monkeypatch):
    import core.surfaces.missed as missed_mod
    monkeypatch.setattr(missed_mod, "missed_notices",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("gone")))
    from core.status_render import render_section_lines
    snap = build_status_snapshot(OWNER, data_dir=degraded)
    lines = "\n".join(render_section_lines(snap, "delivery")).lower()
    assert "could not" in lines or "unreadable" in lines or "unavailable" in lines


# -- what each rail COSTS ------------------------------------------------------
#
# 2026-09-22 14:26Z, OpenRouter below $3: to tell the owner which rail to thin I
# hand-queried `cron_run.spend_usd` out of the telemetry db, because no surface
# answered "what is costing the money". The events have carried the figure all
# along. ⚠️ A run with NO spend recorded is counted as UNPRICED, never as $0 —
# summing an unpriced run into the total would understate the bill, which is the
# confident-zero class this file exists to prevent.

def test_each_rail_reports_what_it_spent_in_24h(tmp_path, monkeypatch):
    data_dir = str(tmp_path)
    monkeypatch.setenv("POLYROB_DATA_DIR", data_dir)
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH",
                       os.path.join(data_dir, "telemetry_events.db"))
    monkeypatch.setenv("CRON_ENABLED", "true")
    _seed_cron(data_dir)
    from core.event_log import TelemetryEventLog
    log = TelemetryEventLog(os.path.join(data_dir, "telemetry_events.db"))
    now = time.time()
    for i, spend in enumerate((0.10, 0.02, 0.03)):
        log.record("cron_run", user_id=OWNER, source="cron",
                   attrs={"job_id": "job1", "outcome": "done", "steps": 5,
                          "duration_s": 60, "spend_usd": spend}, ts=now - 100 - i)
    snap = build_status_snapshot(OWNER, data_dir=data_dir)
    sec = snap.section("loops")
    spend = sec.data["rails_spend_24h"]["job1"]
    assert abs(spend["usd"] - 0.15) < 1e-9
    assert spend["runs"] == 3
    assert spend["unpriced"] == 0
    line = "\n".join(l for l in sec.lines if l.startswith("rail "))
    assert "$0.15" in line and "3 run" in line, line


def test_a_run_with_no_spend_recorded_is_UNPRICED_not_zero(tmp_path, monkeypatch):
    data_dir = str(tmp_path)
    monkeypatch.setenv("POLYROB_DATA_DIR", data_dir)
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH",
                       os.path.join(data_dir, "telemetry_events.db"))
    monkeypatch.setenv("CRON_ENABLED", "true")
    _seed_cron(data_dir)
    from core.event_log import TelemetryEventLog
    log = TelemetryEventLog(os.path.join(data_dir, "telemetry_events.db"))
    now = time.time()
    log.record("cron_run", user_id=OWNER, source="cron",
               attrs={"job_id": "job1", "outcome": "done", "spend_usd": 0.04}, ts=now - 100)
    log.record("cron_run", user_id=OWNER, source="cron",
               attrs={"job_id": "job1", "outcome": "done"}, ts=now - 90)
    snap = build_status_snapshot(OWNER, data_dir=data_dir)
    spend = snap.section("loops").data["rails_spend_24h"]["job1"]
    assert abs(spend["usd"] - 0.04) < 1e-9
    assert spend["runs"] == 2
    assert spend["unpriced"] == 1
    line = "\n".join(l for l in snap.section("loops").lines if l.startswith("rail "))
    assert "unpriced" in line, line


def test_a_skipped_run_costs_nothing_and_says_so(tmp_path, monkeypatch):
    """The $0 preflight skip is the point of the preflight — it must read as a
    real zero, not as an unpriced unknown."""
    data_dir = str(tmp_path)
    monkeypatch.setenv("POLYROB_DATA_DIR", data_dir)
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH",
                       os.path.join(data_dir, "telemetry_events.db"))
    monkeypatch.setenv("CRON_ENABLED", "true")
    _seed_cron(data_dir)
    from core.event_log import TelemetryEventLog
    log = TelemetryEventLog(os.path.join(data_dir, "telemetry_events.db"))
    log.record("cron_run", user_id=OWNER, source="cron",
               attrs={"job_id": "job1", "outcome": "skipped", "reason": "no_slot"},
               ts=time.time() - 60)
    spend = build_status_snapshot(OWNER, data_dir=data_dir).section(
        "loops").data["rails_spend_24h"]["job1"]
    assert spend["usd"] == 0.0 and spend["unpriced"] == 0 and spend["runs"] == 1


def test_a_rail_with_nothing_priced_never_renders_a_confident_zero(tmp_path, monkeypatch):
    """`$0.00` is the lie-shaped string this file bans everywhere else. A rail
    whose runs carry no cost figure has an UNKNOWN cost, not a zero one."""
    data_dir = str(tmp_path)
    monkeypatch.setenv("POLYROB_DATA_DIR", data_dir)
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH",
                       os.path.join(data_dir, "telemetry_events.db"))
    monkeypatch.setenv("CRON_ENABLED", "true")
    _seed_cron(data_dir)
    from core.event_log import TelemetryEventLog
    log = TelemetryEventLog(os.path.join(data_dir, "telemetry_events.db"))
    for i in range(2):
        log.record("cron_run", user_id=OWNER, source="cron",
                   attrs={"job_id": "job1", "outcome": "done"}, ts=time.time() - 60 - i)
    sec = build_status_snapshot(OWNER, data_dir=data_dir).section("loops")
    line = "\n".join(l for l in sec.lines if l.startswith("rail "))
    assert "$0.00" not in line, line
    assert "cost not recorded for 2 runs today" in line, line
