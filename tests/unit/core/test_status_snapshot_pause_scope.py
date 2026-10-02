"""The pause-violation CRITICAL must mean what it says.

⚠️ The defect, from production on 2026-09-23. The owner paused with scope
``trading`` at 18:30:56Z. ``doctor`` then showed:

    ⛔ PAUSE VIOLATED — autonomous activity after the pause: cron_run ×2

Nothing had traded. Checked against ``telemetry_events.db``: wallet/swap/transfer
rows after the pause = **0**, and the two ``cron_run`` rows were ONE job —
``c86c438f5bb5`` (``started`` 18:31:40, ``done`` 18:33:27), the one-shot X post
carrying a tranche report. A post is not a trade.

Two independent bugs produced that line:

1. **No scope check.** ``_effect_violations`` right below it correctly skips an
   effect whose ``pause_kind_for`` scopes miss the active scopes; the loop over
   ``goal_run``/``cron_run``/``self_wake``/``wallet_spend`` did no such test, so
   ANY run after ANY pause was a violation. The file's own comment records the
   same false positive happening before with social posts.
2. **Lifecycle rows counted as runs.** ``started`` and ``done`` are two rows for
   one job, so a single post reported as "×2".

The fix is deliberately ASYMMETRIC, and the asymmetry is the safety property:
only the kinds the ``KIND_SCOPES`` table already describes are narrowed.
``wallet_spend`` and ``goal_run`` stay unconditional, because there is no
defensible mapping for them and guessing one would be guessing in the permissive
direction — on the alarm that guards the owner's pause.
"""
import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AUTONOMY_HALT", raising=False)
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "telemetry_events.db"))
    return tmp_path


def _log(home):
    from core.event_log import TelemetryEventLog
    return TelemetryEventLog(str(home / "telemetry_events.db"))


def _violation(home):
    from core.status_snapshot import build_status_snapshot
    snap = build_status_snapshot("rob", data_dir=str(home), include_money=False)
    v = [h for h in snap.health if h.key == "pause_violation"]
    return v[0].text if v else None


# --- the scope gate -------------------------------------------------------------- #

def test_a_trading_pause_does_not_flag_an_ordinary_cron_run(home):
    """The production case. `KIND_SCOPES['cron_run'] == ('all', 'cron')`, which
    does not intersect ('trading',) — so a cron run is not denied by this pause
    and must not be reported as a breach of it."""
    from core import autonomy_control as ac
    ac.pause(str(home), scopes=("trading",), via="test", set_by="owner")
    _log(home).record("cron_run", user_id="rob", session_id="s1", source="cron",
                      attrs={"job_id": "c86c438f5bb5", "outcome": "started"})
    assert _violation(home) is None


def test_a_cron_pause_does_flag_a_cron_run(home):
    from core import autonomy_control as ac
    ac.pause(str(home), scopes=("cron",), via="test", set_by="owner")
    _log(home).record("cron_run", user_id="rob", session_id="s1", source="cron",
                      attrs={"job_id": "j1", "outcome": "started"})
    out = _violation(home)
    assert out and "cron_run ×1" in out


def test_a_full_pause_still_flags_a_cron_run(home):
    """Regression guard: narrowing must not blind the `all` case."""
    from core import autonomy_control as ac
    ac.pause(str(home), scopes=("all",), via="test", set_by="owner")
    _log(home).record("cron_run", user_id="rob", session_id="s1", source="cron",
                      attrs={"job_id": "j1", "outcome": "started"})
    out = _violation(home)
    assert out and "cron_run ×1" in out


# --- what must NEVER be narrowed ------------------------------------------------- #

def test_a_wallet_spend_is_a_violation_under_any_scope(home):
    """Money moving after the owner said stop is always worth the CRITICAL.
    `wallet_spend` has no KIND_SCOPES entry; it stays unconditional on purpose."""
    from core import autonomy_control as ac
    ac.pause(str(home), scopes=("trading",), via="test", set_by="owner")
    _log(home).record("wallet_spend", user_id="rob", session_id="s1", source="wallet",
                      attrs={"outcome": "done", "usd": 12.5})
    out = _violation(home)
    assert out and "wallet_spend ×1" in out


def test_a_goal_run_is_a_violation_under_any_scope(home):
    """A goal can do anything, and there is no scope that describes it. Leaving
    it unconditional is the fail-safe reading."""
    from core import autonomy_control as ac
    ac.pause(str(home), scopes=("social",), via="test", set_by="owner")
    _log(home).record("goal_run", user_id="rob", session_id="s1", source="goal",
                      attrs={"goal_id": "g1", "outcome": "started"})
    out = _violation(home)
    assert out and "goal_run ×1" in out


# --- counting runs, not rows ------------------------------------------------------ #

def test_one_job_started_and_done_counts_once(home):
    """The production line said ×2 for a single X post."""
    from core import autonomy_control as ac
    ac.pause(str(home), scopes=("all",), via="test", set_by="owner")
    log = _log(home)
    log.record("cron_run", user_id="rob", session_id="s1", source="cron",
               attrs={"job_id": "c86c438f5bb5", "outcome": "started"})
    log.record("cron_run", user_id="rob", session_id="s1", source="cron",
               attrs={"job_id": "c86c438f5bb5", "outcome": "done", "steps": 11})
    out = _violation(home)
    assert out and "cron_run ×1" in out, out


def test_two_distinct_jobs_count_twice(home):
    from core import autonomy_control as ac
    ac.pause(str(home), scopes=("all",), via="test", set_by="owner")
    log = _log(home)
    log.record("cron_run", user_id="rob", session_id="s1", source="cron",
               attrs={"job_id": "j1", "outcome": "started"})
    log.record("cron_run", user_id="rob", session_id="s2", source="cron",
               attrs={"job_id": "j2", "outcome": "started"})
    out = _violation(home)
    assert out and "cron_run ×2" in out


def test_rows_without_an_id_are_still_counted_individually(home):
    """An emitter that carries no id must not collapse to one — that would hide
    real activity. Unknown identity means 'count it', not 'merge it'."""
    from core import autonomy_control as ac
    ac.pause(str(home), scopes=("all",), via="test", set_by="owner")
    log = _log(home)
    log.record("self_wake", user_id="rob", session_id="s1", source="self_wake",
               attrs={"outcome": "started"})
    log.record("self_wake", user_id="rob", session_id="s2", source="self_wake",
               attrs={"outcome": "started"})
    out = _violation(home)
    assert out and "self_wake ×2" in out


# --- the existing contract still holds -------------------------------------------- #

def test_an_outcome_that_honoured_the_pause_is_not_a_violation(home):
    from core import autonomy_control as ac
    ac.pause(str(home), scopes=("all",), via="test", set_by="owner")
    _log(home).record("cron_run", user_id="rob", session_id="s1", source="cron",
                      attrs={"job_id": "j1", "outcome": "paused"})
    assert _violation(home) is None


def test_no_activity_means_no_violation(home):
    from core import autonomy_control as ac
    ac.pause(str(home), scopes=("all",), via="test", set_by="owner")
    assert _violation(home) is None
