"""Per-task aux-model resolution: explicit env wins, then auto-map, else None."""
from agents.task.constants import resolve_aux_model, AUX_MODEL_MAP, _AUX_TASK_ENV


def test_explicit_env_wins(monkeypatch):
    monkeypatch.setenv("AUX_MODEL_JUDGE", "claude-haiku-4-5")
    assert resolve_aux_model("judge", provider="openai") == "claude-haiku-4-5"


def test_auto_map_when_enabled(monkeypatch):
    monkeypatch.delenv("AUX_MODEL_JUDGE", raising=False)
    monkeypatch.setenv("AUX_AUTO", "true")
    assert resolve_aux_model("judge", provider="anthropic") == AUX_MODEL_MAP["anthropic"]


def test_none_when_unconfigured(monkeypatch):
    monkeypatch.delenv("AUX_MODEL_JUDGE", raising=False)
    monkeypatch.delenv("AUX_AUTO", raising=False)
    assert resolve_aux_model("judge", provider="openai") is None


def test_planner_vision_rows_removed(monkeypatch):
    # UP-10 2.1: planner/vision were dead config surface (no call sites) and were
    # removed. With no env mapping they resolve to None regardless of AUX_AUTO.
    assert "planner" not in _AUX_TASK_ENV
    assert "vision" not in _AUX_TASK_ENV
    monkeypatch.setenv("AUX_AUTO", "true")
    monkeypatch.setenv("AUX_MODEL_PLANNER", "should-be-ignored")
    monkeypatch.setenv("AUX_MODEL_VISION", "should-be-ignored")
    assert resolve_aux_model("planner", provider="anthropic") == AUX_MODEL_MAP["anthropic"]
    # ^ note: an unknown task with AUX_AUTO+provider still falls through to the auto-map;
    # the point is the *explicit* AUX_MODEL_PLANNER/VISION envs are no longer consulted.
    monkeypatch.delenv("AUX_AUTO", raising=False)
    assert resolve_aux_model("planner", provider="anthropic") is None
    assert resolve_aux_model("vision", provider="anthropic") is None


def test_compaction_back_compat(monkeypatch):
    # COMPACTION_MODEL must still win for the compaction task (legacy knob).
    monkeypatch.setenv("COMPACTION_MODEL", "gpt-5-mini")
    assert resolve_aux_model("compaction", provider="openai") == "gpt-5-mini"
