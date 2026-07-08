"""W1-1: AUTONOMY_POSTURE — one coherent switch for the shipped-but-dark autonomy loops.

Five flags (completion judge, blocker escalation, self-wake delivery, continuity bridge,
cron) shipped wired but default-OFF behind five independent env vars. AUTONOMY_POSTURE
moves the DEFAULTS of that group together: `silent` (byte-identical to today),
`owner-visible` (verify + owner delivery), `full` (+ cron). An explicit per-flag env
always wins; only the default moves.
"""
import importlib

import pytest

import agents.task.constants as c
import tools.cronjob_tools as cj


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("AUTONOMY_POSTURE", "POLYROB_LOCAL", "ROB_LOCAL",
              "GOAL_COMPLETION_JUDGE", "GOAL_BLOCKER_ESCALATION",
              "GOAL_SELF_WAKE_ENABLED", "AUTONOMOUS_CONTINUITY_BRIDGE", "CRON_ENABLED",
              "WAKE_CHANGE_GATE", "EPISODIC_MEMORY_ENABLED", "EPISODIC_DIGEST_INJECT",
              "REFLECTION_ON_SESSION_CLOSE"):
        monkeypatch.delenv(k, raising=False)
    yield


def test_default_posture_is_silent_byte_identical():
    assert c.autonomy_posture() == "silent"
    # every posture-governed flag stays OFF by default (today's behavior)
    assert c.AutonomyConfig.goal_completion_judge() is False
    assert c.AutonomyConfig.goal_blocker_escalation() is False
    assert c.AutonomyConfig.goal_self_wake_enabled() is False
    assert c.AutonomyConfig.autonomous_continuity_bridge() is False
    assert cj.cron_enabled() is False
    # continuity trio: server-dark in silent (local mode may still flip episodic)
    assert c.AutonomyConfig.episodic_memory_enabled() is False
    assert c.AutonomyConfig.episodic_digest_inject() is False
    assert c.AutonomyConfig.reflection_on_session_close() is False


def test_unknown_posture_degrades_to_silent(monkeypatch):
    monkeypatch.setenv("AUTONOMY_POSTURE", "banana")
    assert c.autonomy_posture() == "silent"
    assert c.AutonomyConfig.goal_completion_judge() is False


def test_owner_visible_turns_on_the_verify_and_deliver_group(monkeypatch):
    monkeypatch.setenv("AUTONOMY_POSTURE", "owner-visible")
    assert c.autonomy_posture() == "owner-visible"
    assert c.AutonomyConfig.goal_completion_judge() is True
    assert c.AutonomyConfig.goal_blocker_escalation() is True
    assert c.AutonomyConfig.goal_self_wake_enabled() is True
    assert c.AutonomyConfig.autonomous_continuity_bridge() is True
    # owner-visible does NOT turn on time-based initiative
    assert cj.cron_enabled() is False
    assert c.AutonomyConfig.wake_change_gate() is False
    # continuity trio joins owner-visible (continuity/learning on the server)
    assert c.AutonomyConfig.episodic_memory_enabled() is True
    assert c.AutonomyConfig.episodic_digest_inject() is True
    assert c.AutonomyConfig.reflection_on_session_close() is True


def test_local_mode_still_flips_episodic_under_silent(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    assert c.autonomy_posture() == "silent"
    assert c.AutonomyConfig.episodic_memory_enabled() is True
    assert c.AutonomyConfig.episodic_digest_inject() is True
    # reflection-on-close is posture-only (not in the safe-local group)
    assert c.AutonomyConfig.reflection_on_session_close() is False


def test_full_adds_cron_on_top(monkeypatch):
    monkeypatch.setenv("AUTONOMY_POSTURE", "full")
    assert c.AutonomyConfig.goal_completion_judge() is True
    assert c.AutonomyConfig.goal_blocker_escalation() is True
    assert cj.cron_enabled() is True
    # full also turns on the cron wake change-gate (it pairs with CRON_ENABLED)
    assert c.AutonomyConfig.wake_change_gate() is True


def test_explicit_per_flag_env_still_wins(monkeypatch):
    # posture=full would turn the judge ON, but an explicit off must win
    monkeypatch.setenv("AUTONOMY_POSTURE", "full")
    monkeypatch.setenv("GOAL_COMPLETION_JUDGE", "off")
    assert c.AutonomyConfig.goal_completion_judge() is False
    # and the inverse: posture=silent but an explicit on must win
    monkeypatch.setenv("AUTONOMY_POSTURE", "silent")
    monkeypatch.setenv("GOAL_BLOCKER_ESCALATION", "true")
    assert c.AutonomyConfig.goal_blocker_escalation() is True


def test_cron_full_posture_default_but_explicit_off_wins(monkeypatch):
    monkeypatch.setenv("AUTONOMY_POSTURE", "full")
    monkeypatch.setenv("CRON_ENABLED", "false")
    assert cj.cron_enabled() is False
