"""Rail preflight — a $0 skip when a DETERMINISTIC precondition is already
false before any model call.

Evidence (intel, 2026-09-21 18:45Z): the SCOUT rail ran 24×/24 h and could not
enter ONCE by its own rule — every run's closing memory read "NO ENTRY — rule 10
slot cap (7 rows vs 6)". $1.07/day, 27 % of the day's spend, re-deriving a fact
the ledger already states. The precondition is readable in-process: the
`## Open positions` row count (core/position_ledger) against the rig's cap. The
EXIT rail's row write is the release — a freed slot changes the count, so the
next tick runs.

Fail-OPEN by construction: an unreadable ledger, an unknown kind or a bad
payload runs the tick. A gate that silently skips a money rail on a read error
would be the 09-21 reconcile outage in another shape.
"""
import pytest

from cron.jobs import CronJob
from cron.preflight import preflight_skip

LEDGER_HEAD = """# Ledger

## Open positions

| Token | Address | Size | Entry | Thesis | Date |
|-------|---------|------|-------|--------|------|
"""


def _row(i):
    return f"| T{i} | 0x{i:040x} | 1.0 | $1 | t | d |\n"


def _ledger(data_dir, rows):
    proj = data_dir / "project"
    proj.mkdir(exist_ok=True)
    (proj / "kb-root-position-ledger.md").write_text(
        LEDGER_HEAD + "".join(_row(i + 1) for i in range(rows)) + "\n## Run log\n- x\n")


def _job(payload=None):
    return CronJob(id="scout1", user_id="u1", task="SCOUT RAIL", schedule_spec="3h",
                   next_run_at=None, payload=payload or {})


def test_no_preflight_never_skips(tmp_path):
    _ledger(tmp_path, 9)
    assert preflight_skip(_job({}), data_dir=str(tmp_path)) is None


def test_slot_cap_skips_when_the_book_is_full(tmp_path):
    _ledger(tmp_path, 7)
    job = _job({"preflight": {"kind": "slot_cap", "max_open_rows": 6}})
    out = preflight_skip(job, data_dir=str(tmp_path))
    assert out is not None
    assert out.reason == "no_slot"
    assert out.attrs == {"preflight": "slot_cap", "open_rows": 7, "max_open_rows": 6}


def test_slot_cap_at_exactly_the_cap_skips(tmp_path):
    _ledger(tmp_path, 6)
    job = _job({"preflight": {"kind": "slot_cap", "max_open_rows": 6}})
    assert preflight_skip(job, data_dir=str(tmp_path)).reason == "no_slot"


def test_slot_cap_runs_when_a_slot_is_free(tmp_path):
    """The EXIT rail's row write is the release: fewer rows → the next tick runs."""
    _ledger(tmp_path, 5)
    job = _job({"preflight": {"kind": "slot_cap", "max_open_rows": 6}})
    assert preflight_skip(job, data_dir=str(tmp_path)) is None


def test_unreadable_ledger_fails_open(tmp_path):
    job = _job({"preflight": {"kind": "slot_cap", "max_open_rows": 6}})
    assert preflight_skip(job, data_dir=str(tmp_path)) is None  # no ledger file at all


def test_unknown_kind_or_bad_cap_fails_open(tmp_path):
    _ledger(tmp_path, 9)
    assert preflight_skip(_job({"preflight": {"kind": "moon_phase"}}), data_dir=str(tmp_path)) is None
    assert preflight_skip(_job({"preflight": {"kind": "slot_cap", "max_open_rows": "six"}}),
                          data_dir=str(tmp_path)) is None
    assert preflight_skip(_job({"preflight": {"kind": "slot_cap", "max_open_rows": 0}}),
                          data_dir=str(tmp_path)) is None
    assert preflight_skip(_job({"preflight": "slot_cap"}), data_dir=str(tmp_path)) is None


def test_delivery_jobs_are_never_preflighted(tmp_path):
    _ledger(tmp_path, 9)
    job = _job({"preflight": {"kind": "slot_cap", "max_open_rows": 6}, "deliver": "telegram"})
    assert preflight_skip(job, data_dir=str(tmp_path)) is None


# --- runner wiring: the skip is a durable $0 tick, the agent is never built ---

@pytest.mark.asyncio
async def test_runner_records_no_slot_and_never_builds_a_session(tmp_path, monkeypatch):
    from core import event_log as el
    from cron.runner import make_agent_runner
    monkeypatch.setenv("CRON_RUN_LOOP", "true")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(el, "_INSTANCES", {})
    log = el.TelemetryEventLog(str(tmp_path / "te.db"))
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: log)
    monkeypatch.setattr(el, "event_log_enabled", lambda: True)
    _ledger(tmp_path, 7)
    calls = []

    class _TaskAgent:
        async def create_session(self, *a, **k):
            calls.append("create"); return {"id": "s1"}

        async def run_session(self, *a, **k):
            calls.append("run"); return "x"

    runner = make_agent_runner(_TaskAgent(), data_dir=str(tmp_path))
    ok = await runner(_job({"preflight": {"kind": "slot_cap", "max_open_rows": 6},
                            "priority": "money", "rig": "money_rail"}))
    assert ok is True
    assert calls == []
    rows = log.query(kind="cron_run")
    assert [(r["attrs"]["outcome"], r["attrs"]["reason"]) for r in rows] == [("skipped", "no_slot")]
    assert rows[0]["attrs"]["open_rows"] == 7 and rows[0]["attrs"]["max_open_rows"] == 6
