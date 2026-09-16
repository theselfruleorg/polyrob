"""C6 (2026-09-15 prod review): a run-START ping is not owner information.

`source="self_evolution"` produced 223 delivery attempts in 7 days and 29 of
them reached the owner — 87% generated only to be thrown away, each costing a
telemetry write and (until C3) a row in the owner's `/missed` store. The board
and the console already show that a goal started; a Telegram ping per start is
volume, and it was ON by default at `AUTONOMY_POSTURE=full`.

Completion notices are NOT touched: "what did it produce" is the report.
"""
import pytest


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("AUTONOMY_START_NOTICE", "AUTONOMY_POSTURE", "AUTONOMY_MODE",
              "POLYROB_LOCAL"):
        monkeypatch.delenv(k, raising=False)


def _start_notice():
    from agents.task.constants import AutonomyConfig
    return AutonomyConfig.autonomy_start_notice()


def test_start_notice_is_off_at_posture_full(monkeypatch):
    monkeypatch.setenv("AUTONOMY_POSTURE", "full")
    assert _start_notice() is False


def test_start_notice_is_off_by_default(monkeypatch):
    assert _start_notice() is False


def test_owner_can_still_opt_in(monkeypatch):
    monkeypatch.setenv("AUTONOMY_POSTURE", "full")
    monkeypatch.setenv("AUTONOMY_START_NOTICE", "true")
    assert _start_notice() is True


def test_posture_full_still_carries_its_other_flags(monkeypatch):
    """The demotion is one flag, not a posture change."""
    monkeypatch.setenv("AUTONOMY_POSTURE", "full")
    from core.config_policy.autonomy_posture import _POSTURE_FULL_FLAGS
    assert "CRON_ENABLED" in _POSTURE_FULL_FLAGS
    assert "AUTONOMY_START_NOTICE" not in _POSTURE_FULL_FLAGS
