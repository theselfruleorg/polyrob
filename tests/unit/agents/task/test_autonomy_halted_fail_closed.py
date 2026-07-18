"""H6 leg 3 (2026-07-15): `AutonomyConfig.autonomy_halted()` must fail CLOSED.

The kill-switch file probe used to `except Exception: pass` and report "not halted"
if the halt-file check raised — a fail-OPEN hole on a money path. It now treats an
unprovable state as halted. The $0/no-file/no-env default remains exactly ``False``
(the exception branch never runs in normal operation, so the default envelope is
byte-identical).
"""
import os


def test_autonomy_halted_false_at_defaults(tmp_path, monkeypatch):
    """No env, no halt file at the resolved data home -> not halted (byte-identical)."""
    monkeypatch.delenv("AUTONOMY_HALT", raising=False)
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.delenv("DATA_ROOT", raising=False)
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    from agents.task.constants import AutonomyConfig
    assert AutonomyConfig.autonomy_halted() is False


def test_autonomy_halted_fails_closed_on_probe_exception(tmp_path, monkeypatch):
    """If the halt-file check raises (cannot prove NOT-halted) -> treat as HALTED."""
    monkeypatch.delenv("AUTONOMY_HALT", raising=False)
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.delenv("DATA_ROOT", raising=False)
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)

    real_exists = os.path.exists

    def _boom(p):
        # Only the halt-file probe blows up; everything else is untouched.
        if str(p).endswith("AUTONOMY_HALT"):
            raise OSError("simulated halt-file probe failure")
        return real_exists(p)

    monkeypatch.setattr(os.path, "exists", _boom)
    from agents.task.constants import AutonomyConfig
    assert AutonomyConfig.autonomy_halted() is True  # fail CLOSED


def test_autonomy_halted_env_flag_still_wins(monkeypatch):
    """The explicit env lever is unchanged and short-circuits before any file probe."""
    monkeypatch.setenv("AUTONOMY_HALT", "true")
    from agents.task.constants import AutonomyConfig
    assert AutonomyConfig.autonomy_halted() is True
