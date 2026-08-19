"""026 P0.5 — `/autonomy` honesty: arg error + the four missing rows.

The gap (G1): `/autonomy on` printed the status panel with no error, and the
panel omitted AUTONOMY_MODE, AUTONOMY_POSTURE, CRON_ENABLED and AUTONOMY_HALT
(the one knob that IS live-togglable).
"""
import pytest

from cli.ui.commands.handlers import _autonomy_snapshot, _h_autonomy
from cli.ui.commands.registry import CommandContext


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for var in ("AUTONOMY_MODE", "AUTONOMY_POSTURE", "AUTONOMY_ENABLED",
                "POLYROB_LOCAL", "ROB_LOCAL", "AUTONOMY_HALT", "CRON_ENABLED"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))


class _Emit:
    def __init__(self):
        self.messages = []

    def __call__(self, text, title=None):
        self.messages.append((title, text))


def _ctx(args=None):
    ctx = CommandContext(args=list(args or []))
    emit = _Emit()
    ctx.emit = emit
    return ctx, emit


def test_snapshot_carries_mode_posture_halt_cron():
    snap = _autonomy_snapshot("local", "data")
    assert snap["mode_display"] == "supervised"
    assert snap["posture"] == "silent"
    assert snap["halted"] is False
    assert "cron" in dict(snap["flags"])


def test_autonomy_panel_shows_new_rows():
    ctx, emit = _ctx()
    _h_autonomy(ctx)
    (_title, text), = emit.messages
    assert "mode" in text and "supervised" in text
    assert "posture" in text and "silent" in text
    assert "halt" in text
    assert "AUTONOMY_ENABLED" in text  # the write-path footer


def test_autonomy_args_error_names_the_write_path():
    ctx, emit = _ctx(args=["on"])
    _h_autonomy(ctx)
    (_title, text), = emit.messages
    assert "takes no arguments" in text
    assert "AUTONOMY_ENABLED" in text
    assert "owner halt" in text


def test_autonomy_halted_row(monkeypatch):
    monkeypatch.setenv("AUTONOMY_HALT", "1")
    ctx, emit = _ctx()
    _h_autonomy(ctx)
    (_title, text), = emit.messages
    assert "HALTED" in text
    assert "owner resume" in text
