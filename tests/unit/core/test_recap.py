"""core.recap — surface-neutral recap core (owner-UX P4 T1).

Extracted from ``cli/ui/commands/h_journey.py`` so ``/journey`` (and any
future recap surface) share ONE data-gathering implementation instead of each
surface re-querying episodes/events/skills/ledger itself. See
``tests/unit/cli/ui/test_h_journey.py`` for the rendering-layer tests (those
monkeypatch ``build_recap`` directly — the h_journey/core boundary).
"""
import time

import pytest

from core.recap import RecapEntry, build_recap, format_recap_markdown, _parse_window


@pytest.fixture(autouse=True)
def _reset_skill_usage_singleton():
    # get_skill_usage_store() is a process-wide singleton keyed by the first
    # data_dir it's called with (modules/skills/skill_usage.py) — reset it
    # around each test so tmp_path fixtures here never leak across tests.
    from modules.skills.skill_usage import reset_skill_usage_store
    reset_skill_usage_store()
    yield
    reset_skill_usage_store()


# --- window parsing -----------------------------------------------------------

def test_parse_window_valid_durations():
    assert _parse_window("30m") == 30 * 60
    assert _parse_window("24h") == 24 * 3600
    assert _parse_window("7d") == 7 * 86400
    assert _parse_window("3600") == 3600.0


def test_parse_window_unset_means_all_time():
    assert _parse_window("") is None
    assert _parse_window(None) is None


def test_parse_window_malformed_raises_value_error():
    with pytest.raises(ValueError):
        _parse_window("junk")
    with pytest.raises(ValueError):
        _parse_window("24x")


def test_parse_window_rejects_non_finite_and_absurd_windows():
    """Owner-UX P4 T4 review hardening: a window that parses to something
    unusable (inf/nan/absurdly-large) must raise the same friendly ValueError
    as a malformed label — not silently fall through to whatever int(nan) or
    a 1e34-second query happens to do downstream. Telegram /recap exposes
    the raw label to chat input, so this is the front-line guard."""
    with pytest.raises(ValueError):
        _parse_window("1e400d")  # overflows to inf
    with pytest.raises(ValueError):
        _parse_window("nand")  # "nan" + the 'd' suffix -> float("nan")
    with pytest.raises(ValueError):
        _parse_window("9" * 30 + "d")  # a 30-digit day count — way over 10y
    # unaffected: ordinary windows still parse exactly as before.
    assert _parse_window("24h") == 24 * 3600
    assert _parse_window("7d") == 7 * 86400


def test_parse_window_rejects_non_positive_windows():
    """Review follow-up: a negative window ('-24h' -> -86400.0) is finite and
    under the max bound, but makes build_recap compute a FUTURE since_ts —
    a silent 'Nothing to report' instead of the promised ValueError. Zero is
    equally meaningless as a duration. Both must raise from _parse_window."""
    with pytest.raises(ValueError):
        _parse_window("-24h")
    with pytest.raises(ValueError):
        _parse_window("-1d")
    with pytest.raises(ValueError):
        _parse_window("0h")
    # positives unchanged
    assert _parse_window("30m") == 30 * 60
    assert _parse_window("24h") == 24 * 3600


def test_build_recap_malformed_window_raises_clear_error(tmp_path):
    with pytest.raises(ValueError):
        build_recap("u1", str(tmp_path), window="not-a-window")


# --- seeded event/goal/skill fixtures -----------------------------------------

def test_build_recap_reads_seeded_event_and_skill_fixtures(tmp_path, monkeypatch):
    """Real rows in a real (tmp_path) telemetry_events.db + skill_usage.db —
    not monkeypatched sources — flow through build_recap with correct kinds."""
    from agents.task.telemetry import event_log as el
    from modules.skills.skill_usage import get_skill_usage_store

    home = str(tmp_path)
    now = time.time()

    log = el.TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    log.record("self_modification", user_id="u1", ts=now,
               attrs={"kind": "skill", "skill_id": "s1"})
    log.record("goal_run", user_id="u1", ts=now, outcome="done", goal_id="g1")
    log.record("goal_run", user_id="u2", ts=now, outcome="done")  # other tenant, excluded

    store = get_skill_usage_store(home)
    store.record_provenance("s1", "u1", "agent")
    store.bump_load("s1", "u1")
    store.bump_load("s1", "u1")

    # Keep episodes/ledger out of scope for this test (separate infra/registries).
    monkeypatch.setattr("core.recap._episodes", lambda *a, **k: [])
    monkeypatch.setattr("core.recap._ledger", lambda *a, **k: {})

    entries = build_recap("u1", home, window="24h")
    kinds = {e.kind for e in entries}
    assert "self_modification" in kinds
    assert "goal_run" in kinds
    assert "skill" in kinds

    sm = next(e for e in entries if e.kind == "self_modification")
    assert "skill s1" in sm.text

    skill = next(e for e in entries if e.kind == "skill")
    assert "s1" in skill.text and "used 2x" in skill.text

    # tenant scoping — u2's goal_run must never leak into u1's recap.
    assert all(e.kind != "goal_run" or "u2" not in e.text for e in entries)


def test_build_recap_filters_events_by_window(tmp_path, monkeypatch):
    from agents.task.telemetry import event_log as el

    home = str(tmp_path)
    now = time.time()
    log = el.TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    log.record("goal_run", user_id="u1", ts=now, outcome="done")            # in-window
    log.record("goal_run", user_id="u1", ts=now - 999_999, outcome="done")  # long expired

    monkeypatch.setattr("core.recap._episodes", lambda *a, **k: [])
    monkeypatch.setattr("core.recap._authored", lambda *a, **k: [])
    monkeypatch.setattr("core.recap._ledger", lambda *a, **k: {})

    entries = build_recap("u1", home, window="1h")
    assert len(entries) == 1


# --- empty-window / no-activity behavior --------------------------------------

def test_build_recap_no_activity_returns_empty_list(tmp_path, monkeypatch):
    monkeypatch.setattr("core.recap._episodes", lambda *a, **k: [])
    monkeypatch.setattr("core.recap._events", lambda *a, **k: [])
    monkeypatch.setattr("core.recap._authored", lambda *a, **k: [])
    monkeypatch.setattr("core.recap._ledger", lambda *a, **k: {})

    entries = build_recap("u1", str(tmp_path), window="24h")
    assert entries == []


def test_format_recap_markdown_empty_state_is_friendly():
    out = format_recap_markdown([], "24h")
    assert "24h" in out
    assert "Nothing to report" in out


# --- markdown formatter shape --------------------------------------------------

def test_format_recap_markdown_shape():
    entries = [
        RecapEntry(ts=2.0, kind="ledger", text="Earned: $1.00 (1 settled)", amount=1.0),
        RecapEntry(ts=1.0, kind="goal_run", text="done", amount=None),
    ]
    out = format_recap_markdown(entries, "7d")
    lines = out.splitlines()
    assert lines[0].startswith("# Recap")
    assert "7d" in lines[0]
    body = [l for l in lines if l.startswith("-")]
    assert len(body) == 2
    assert any("ledger" in l and "Earned: $1.00" in l for l in body)
    assert any("goal_run" in l and "done" in l for l in body)


def test_format_recap_markdown_no_window_is_all_time():
    out = format_recap_markdown([], "")
    assert "all time" in out


# --- ledger seam loop-safety (moved from tests/unit/cli/ui/test_h_journey.py) --

def test_ledger_seam_works_inside_running_loop(monkeypatch):
    # Regression: build_recap's _ledger seam may be invoked from a caller
    # already inside a running event loop (e.g. the REPL dispatch), where a
    # bare asyncio.run() would raise -> fail-open silently empties the money
    # section. The loop-safe bridge (core.async_bridge.run_coroutine_sync)
    # must still return the real ledger even under a running loop.
    import asyncio
    from core.recap import _ledger

    async def _fake_build(user_id, *, days=7, db=None, include_balances=False):
        return {"earned_usd": 4.0, "total_spend_usd": 1.0, "net_usd": 3.0}

    monkeypatch.setattr("modules.credits.unified_ledger.build_ledger", _fake_build)

    async def _driver():
        # calling the SYNC seam from within a running loop (mirrors the REPL)
        return _ledger("u1", 7)

    result = asyncio.run(_driver())
    assert result.get("earned_usd") == 4.0  # NOT {} from a swallowed RuntimeError


def _split_ledger(**over):
    """A ledger_rollup()-shaped dict with the split treasury/runtime blocks
    (Tasks 1-4) plus the legacy flat availability keys ``ledger_availability_note``
    still reads. ``treasury.net_usd`` is always ``income_usd - spend_usd`` —
    never the legacy merged figure."""
    led = {
        "user_id": "rob", "window_days": 1,
        "treasury": {"income_usd": 0.0, "spend_usd": 0.0, "pending_usd": 0.0,
                     "pending_count": 0, "balance_usd": None, "net_usd": 0.0,
                     "available": True},
        "runtime": {"spend_window_usd": 0.0, "spend_total_usd": 0.0,
                    "calls_window": 0, "calls_total": 0,
                    "provider_balance_usd": None, "available": True},
        "costs_available": True, "inbound_available": True, "wallet_metering": "on",
        "settled_payments": 0,
    }
    led.update(over)
    return led


def test_all_zero_ledger_produces_no_entry(monkeypatch, tmp_path):
    """A $0.00/$0.00/0-settled ledger is NOT activity: with the global DB
    initialized, build_ledger succeeds with all zeros on a fresh tenant and
    used to inject a noise entry — defeating the 'nothing to report' branch
    (order-dependent test poison + a pointless $0 line on Telegram /recap).
    h_journey renders its own $0 fallback line, so /journey is unaffected."""
    from core import recap
    monkeypatch.setattr(recap, "_ledger", lambda uid, days: _split_ledger())
    entries = recap.build_recap("u-zero", data_home=str(tmp_path), window="24h")
    assert [e for e in entries if e.kind == "ledger"] == []
    assert "nothing to report" in recap.format_recap_markdown(entries, "24h").lower()


def test_nonzero_ledger_still_produces_entry(monkeypatch, tmp_path):
    from core import recap
    monkeypatch.setattr(recap, "_ledger", lambda uid, days: _split_ledger(
        treasury={"income_usd": 2.0, "spend_usd": 0.5, "pending_usd": 0.0,
                  "pending_count": 0, "balance_usd": None, "net_usd": 1.5,
                  "available": True},
        settled_payments=1))
    entries = recap.build_recap("u-live", data_home=str(tmp_path), window="24h")
    ledger_entries = [e for e in entries if e.kind == "ledger"]
    assert len(ledger_entries) == 1
    assert "$2.00" in ledger_entries[0].text


def test_runtime_only_activity_still_produces_entry(monkeypatch, tmp_path):
    """Regression: the guard at core/recap.py:211 includes both `runtime` and
    `calls` in the any(...) tuple. Before they were added, a tenant with only
    API-spend activity (zero treasury metrics) would silently vanish from the
    recap. This test verifies the guard catches the runtime-only case."""
    from core import recap
    monkeypatch.setattr(recap, "_ledger", lambda uid, days: _split_ledger(
        runtime={"spend_window_usd": 2.0, "spend_total_usd": 0.0,
                 "calls_window": 10, "calls_total": 0,
                 "provider_balance_usd": None, "available": True}))
    entries = recap.build_recap("u-runtime-only", data_home=str(tmp_path), window="24h")
    ledger_entries = [e for e in entries if e.kind == "ledger"]
    assert len(ledger_entries) == 1, "runtime-only activity must produce a ledger entry"
    assert "runtime $2.00" in ledger_entries[0].text


def test_degraded_ledger_entry_carries_availability_note(monkeypatch, tmp_path):
    """H14b: a partially-degraded rollup (real earnings but a leg that could not
    be read) must annotate the entry — its zeroed legs are not real $0.00."""
    from core import recap
    monkeypatch.setattr(recap, "_ledger", lambda uid, days: _split_ledger(
        treasury={"income_usd": 2.0, "spend_usd": 0.0, "pending_usd": 0.0,
                  "pending_count": 0, "balance_usd": None, "net_usd": 2.0,
                  "available": False},
        settled_payments=1, costs_available=False,
        inbound_available=True, wallet_metering="on"))
    entries = recap.build_recap("u-degraded", data_home=str(tmp_path), window="24h")
    ledger_entries = [e for e in entries if e.kind == "ledger"]
    assert len(ledger_entries) == 1
    assert "metering" in ledger_entries[0].text.lower()
    assert "⚠" in ledger_entries[0].text


def test_recap_entry_reports_treasury_and_runtime_separately(monkeypatch):
    """The 2026-07-16 bug: recap read the merged total_spend_usd/net_usd and
    rendered 'Earned: $X · spent $Y · net $Z', reporting the owner's API bill
    as the agent's own P&L. Treasury and runtime must never be summed."""
    from core.recap import build_recap
    monkeypatch.setattr("core.recap._ledger", lambda u, d: {
        "user_id": "rob", "window_days": 1,
        "treasury": {"income_usd": 4.0, "spend_usd": 1.0, "pending_usd": 0.0,
                     "pending_count": 0, "balance_usd": None, "net_usd": 3.0,
                     "available": True},
        "runtime": {"spend_window_usd": 2.0, "spend_total_usd": 9.0,
                    "calls_window": 10, "calls_total": 40,
                    "provider_balance_usd": None, "available": True},
        "costs_available": True, "inbound_available": True, "wallet_metering": "on",
        "settled_payments": 1,
    })
    entries = build_recap("rob")
    ledger_entries = [e for e in entries if e.kind == "ledger"]  # real kind label
    assert len(ledger_entries) == 1
    text = ledger_entries[0].text
    assert "Income: $4.00" in text
    assert "net $3.00" in text          # income - spend, runtime excluded
    assert "runtime $2.00" in text
    assert "$-2.00" not in text
    assert "earned" not in text.lower()  # terminology is income/spend, never "earned"


def test_recap_never_probes_balances(monkeypatch):
    """recap is an ACTIVITY CHECK on a sync bridge — it must never hit the network."""
    import core.activity_evidence as ae
    seen = {}

    async def fake_build(user_id, *, days=7, include_balances=False, db=None):
        seen["include_balances"] = include_balances
        return {}
    monkeypatch.setattr("modules.credits.unified_ledger.build_ledger", fake_build)
    ae.ledger_rollup("rob", 1)
    assert seen["include_balances"] is False
