"""Tests for the POLYROB_LOCAL terminal-native profile (safe autonomy flags ON as a group).

The flag readers (`local_mode_enabled`, `AutonomyConfig.*_enabled`) read the environment
LIVE at call time via `_bool_env`/`os.getenv`, so these tests just monkeypatch env and call
the functions directly. Do NOT `importlib.reload(constants)` here — reloading swaps the
global module object and desyncs other tests that imported names from it (it caused
order-dependent failures in unrelated suites).
"""
import agents.task.constants as constants


def test_rob_local_alias_enables_local_mode(monkeypatch):
    # ROB_LOCAL is a deprecated back-compat alias for POLYROB_LOCAL (older docs/scripts
    # said ROB_LOCAL) — it must NOT be a silent no-op.
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setenv("ROB_LOCAL", "1")
    assert constants.local_mode_enabled() is True


def test_local_mode_off_by_default(monkeypatch):
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("ROB_LOCAL", raising=False)
    monkeypatch.delenv("GOALS_ENABLED", raising=False)
    monkeypatch.delenv("CURATOR_ENABLED", raising=False)
    monkeypatch.delenv("SELF_WAKE_ENABLED", raising=False)
    assert constants.local_mode_enabled() is False
    # safe flags stay OFF when not local
    assert constants.AutonomyConfig.goals_enabled() is False
    assert constants.AutonomyConfig.curator_enabled() is False
    assert constants.AutonomyConfig.self_wake_enabled() is False


_AUTONOMY_FLAG_READERS = (
    ("GOALS_ENABLED", constants.AutonomyConfig.goals_enabled),
    ("CURATOR_ENABLED", constants.AutonomyConfig.curator_enabled),
    ("SELF_WAKE_ENABLED", constants.AutonomyConfig.self_wake_enabled),
    ("SKILLS_WRITABLE", constants.AutonomyConfig.skills_writable),
    ("BACKGROUND_REVIEW_ENABLED", constants.AutonomyConfig.background_review_enabled),
    ("INSIGHTS_TOOL", constants.AutonomyConfig.insights_tool),
    ("GOAL_PLANNER_ENABLED", constants.AutonomyConfig.goal_planner_enabled),
    ("KNOWLEDGE_CURATOR_ENABLED", constants.AutonomyConfig.knowledge_curator_enabled),
    ("SELF_CONTEXT_WRITABLE", constants.AutonomyConfig.self_context_writable),
    ("OWNER_DOC_WRITABLE", constants.AutonomyConfig.owner_doc_writable),
    ("SELF_EVOLUTION_TRANSPARENCY", constants.AutonomyConfig.self_evolution_transparency),
    ("CONTINUITY_BRIDGE_ENABLED", constants.AutonomyConfig.continuity_bridge_enabled),
    ("DELIVERABLES_ATTACH_ENABLED", constants.AutonomyConfig.deliverables_attach_enabled),
)

_INTERACTIVE_FLAG_READERS = (
    ("KB_ENABLED", constants.AutonomyConfig.kb_enabled),
    ("AGENT_STATUS_TOOL", constants.AutonomyConfig.agent_status_tool),
    ("MESSAGE_TOOL_ENABLED", constants.message_tool_enabled),
    ("PREFS_TOOL_ENABLED", constants.prefs_tool_enabled),
    ("PROJECT_CONTEXT_AUTOLOAD", constants.AutonomyConfig.project_context_autoload),
    ("TOOL_PROGRESSIVE_DISCLOSURE", constants.tool_progressive_disclosure),
)


def _clear(monkeypatch, *names):
    for n in names:
        monkeypatch.delenv(n, raising=False)


def test_local_mode_flips_interactive_flags_on_but_not_autonomy(monkeypatch):
    # 0.9.0 split: POLYROB_LOCAL alone flips the INTERACTIVE bucket ON, but the
    # self-directed AUTONOMY loops stay OFF until AUTONOMY_ENABLED is set.
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    _clear(monkeypatch, "AUTONOMY_ENABLED", "AUTONOMY_POSTURE", "AUTONOMY_MODE")
    for name, _ in _AUTONOMY_FLAG_READERS + _INTERACTIVE_FLAG_READERS:
        monkeypatch.delenv(name, raising=False)
    assert constants.local_mode_enabled() is True
    assert constants.autonomy_enabled() is False
    for name, reader in _INTERACTIVE_FLAG_READERS:
        assert reader() is True, f"interactive {name} should be ON under POLYROB_LOCAL"
    for name, reader in _AUTONOMY_FLAG_READERS:
        assert reader() is False, f"autonomy {name} must stay OFF without AUTONOMY_ENABLED"


def test_autonomy_group_needs_master_flag(monkeypatch):
    # POLYROB_LOCAL + AUTONOMY_ENABLED=1 => the whole autonomy bucket flips ON.
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("AUTONOMY_ENABLED", "1")
    _clear(monkeypatch, "AUTONOMY_POSTURE", "AUTONOMY_MODE")
    for name, _ in _AUTONOMY_FLAG_READERS:
        monkeypatch.delenv(name, raising=False)
    assert constants.autonomy_enabled() is True
    for name, reader in _AUTONOMY_FLAG_READERS:
        assert reader() is True, f"autonomy {name} should be ON under local+AUTONOMY_ENABLED"


def test_autonomy_enabled_needs_local_too(monkeypatch):
    # The master flag alone (no POLYROB_LOCAL) does NOT flip the group — the flags
    # are the LOCAL profile's autonomous subset (local AND autonomy).
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("ROB_LOCAL", raising=False)
    monkeypatch.setenv("AUTONOMY_ENABLED", "1")
    _clear(monkeypatch, "AUTONOMY_POSTURE", "AUTONOMY_MODE", "GOALS_ENABLED")
    assert constants.autonomy_enabled() is True
    assert constants.AutonomyConfig.goals_enabled() is False


def test_autonomy_enabled_default_on_under_posture(monkeypatch):
    # A deliberate autonomous posture makes the master default ON, so the autonomy
    # layer is never silently inert when the operator asked for it.
    _clear(monkeypatch, "AUTONOMY_ENABLED", "AUTONOMY_MODE")
    monkeypatch.setenv("AUTONOMY_POSTURE", "owner-visible")
    assert constants.autonomy_enabled() is True
    monkeypatch.setenv("AUTONOMY_POSTURE", "full")
    assert constants.autonomy_enabled() is True
    monkeypatch.setenv("AUTONOMY_POSTURE", "silent")
    assert constants.autonomy_enabled() is False


def test_explicit_autonomy_enabled_off_beats_posture(monkeypatch):
    _clear(monkeypatch, "AUTONOMY_MODE")
    monkeypatch.setenv("AUTONOMY_POSTURE", "full")
    monkeypatch.setenv("AUTONOMY_ENABLED", "off")
    assert constants.autonomy_enabled() is False


def test_explicit_flag_on_wins_without_local_or_master(monkeypatch):
    # A single explicit per-flag env still works with neither local nor the master.
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("ROB_LOCAL", raising=False)
    _clear(monkeypatch, "AUTONOMY_ENABLED", "AUTONOMY_POSTURE", "AUTONOMY_MODE")
    monkeypatch.setenv("GOALS_ENABLED", "1")
    assert constants.AutonomyConfig.goals_enabled() is True


def test_explicit_off_overrides_local(monkeypatch):
    # an explicit disable must beat the (local + AUTONOMY_ENABLED) default
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("AUTONOMY_ENABLED", "1")
    monkeypatch.setenv("GOALS_ENABLED", "off")
    assert constants.AutonomyConfig.goals_enabled() is False


def test_server_both_buckets_off(monkeypatch):
    # Server byte-identity: no POLYROB_LOCAL => interactive AND autonomy buckets off.
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("ROB_LOCAL", raising=False)
    _clear(monkeypatch, "AUTONOMY_ENABLED", "AUTONOMY_POSTURE", "AUTONOMY_MODE")
    for name, _ in _AUTONOMY_FLAG_READERS + _INTERACTIVE_FLAG_READERS:
        monkeypatch.delenv(name, raising=False)
    for name, reader in _INTERACTIVE_FLAG_READERS + _AUTONOMY_FLAG_READERS:
        assert reader() is False, f"{name} must be OFF on a plain server"


def test_code_exec_not_flipped_by_local(monkeypatch):
    # code execution is NOT a safe-local flag — local mode must never enable it
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.delenv("CODE_EXEC_ENABLED", raising=False)
    assert "CODE_EXEC_ENABLED" not in constants._SAFE_LOCAL_FLAGS


def test_ticker_idle_backoff_off_by_default(monkeypatch):
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("TICKER_IDLE_BACKOFF_ENABLED", raising=False)
    assert constants.ticker_idle_backoff_enabled() is False


def test_ticker_idle_backoff_on_under_local_mode(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.delenv("TICKER_IDLE_BACKOFF_ENABLED", raising=False)
    assert constants.ticker_idle_backoff_enabled() is True


def test_ticker_idle_backoff_explicit_off_beats_local_mode(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("TICKER_IDLE_BACKOFF_ENABLED", "off")
    assert constants.ticker_idle_backoff_enabled() is False


def test_ticker_idle_backoff_max_multiplier_default(monkeypatch):
    monkeypatch.delenv("TICKER_IDLE_BACKOFF_MAX_MULTIPLIER", raising=False)
    assert constants.ticker_idle_backoff_max_multiplier() == 5


def test_ticker_idle_backoff_max_multiplier_explicit(monkeypatch):
    monkeypatch.setenv("TICKER_IDLE_BACKOFF_MAX_MULTIPLIER", "3")
    assert constants.ticker_idle_backoff_max_multiplier() == 3
