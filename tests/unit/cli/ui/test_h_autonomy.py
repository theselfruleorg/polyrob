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
        # setenv first so monkeypatch RESTORES the var at teardown — a live
        # `/autonomy on` writes os.environ directly (026 P5).
        monkeypatch.setenv(var, "")
        monkeypatch.delenv(var)
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


def test_loop_table_has_nine_loop_rows():
    """043 A8/A42 — the panel names all 8 loops `start_autonomy` can start
    (cron/goals/curator/sandbox-reap/surface-gc/quiet-release/settlement/
    bridges) plus the planner, matching the liveness set the status snapshot
    checks (core/autonomy_runtime.py::start_autonomy)."""
    snap = _autonomy_snapshot("local", "data")
    names = dict(snap["flags"])
    loop_rows = {"cron", "goals", "curator", "sandbox-reap", "surface-gc",
                "quiet-release", "settlement", "bridges", "planner"}
    missing = loop_rows - set(names)
    assert not missing, f"loop rows missing from the /autonomy panel: {missing}"


def test_autonomy_panel_shows_new_rows():
    ctx, emit = _ctx()
    _h_autonomy(ctx)
    (_title, text), = emit.messages
    assert "mode" in text and "supervised" in text
    assert "posture" in text and "silent" in text
    assert "pause" in text and "RUNNING" in text  # 031: the shared pause headline
    # E18: the footer names the VERB, not the flag.
    assert "polyrob autonomy on" in text
    assert "AUTONOMY_ENABLED" not in text


def test_autonomy_args_error_names_the_write_path():
    ctx, emit = _ctx(args=["junk"])
    _h_autonomy(ctx)
    (_title, text), = emit.messages
    assert "takes `on`, `off` or nothing" in text
    # E18: the ONE remedy, named as a verb.
    assert "polyrob autonomy on" in text
    assert "AUTONOMY_ENABLED" not in text
    assert "/pause" in text and "/resume" in text  # 031: the live pause verbs


def test_autonomy_halted_row(monkeypatch):
    monkeypatch.setenv("AUTONOMY_HALT", "1")
    ctx, emit = _ctx()
    _h_autonomy(ctx)
    (_title, text), = emit.messages
    assert "PAUSED (everything)" in text  # the legacy env facet reads as a full pause
    # 030 WS-C2: the REPL now names its own seat-local verb.
    assert "/resume" in text


# ---------------------------------------------------------------------------
# 026 P5.2 — `/autonomy on|off` switch the loops live
# ---------------------------------------------------------------------------

class _Handles:
    def __init__(self, enabled):
        self.autonomy_enabled = enabled
        self.stopped = False

    async def stop(self):
        self.stopped = True


@pytest.fixture
def live(monkeypatch, tmp_path):
    import os
    from cli.ui import autonomy_live
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "home"))
    started = []

    def _start():
        h = _Handles(os.environ.get("AUTONOMY_ENABLED") == "true")
        started.append(h)
        return h

    ctl = autonomy_live.LiveAutonomy(_start)
    ctl.start()
    autonomy_live.bind(ctl)
    yield ctl, started
    autonomy_live.bind(None)


@pytest.mark.asyncio
async def test_autonomy_on_applies_live_and_restarts_the_loops(live):
    import os
    ctl, started = live
    ctx, emit = _ctx(args=["on"])
    await _h_autonomy(ctx)
    (_title, text), = emit.messages
    assert os.environ["AUTONOMY_ENABLED"] == "true"
    assert "live in this session" in text and "started in this session" in text
    assert started[0].stopped is True and len(started) == 2
    assert ctl.handles is started[1] and ctl.handles.autonomy_enabled is True
    from core.config_policy import autonomy_enabled
    assert autonomy_enabled() is True


@pytest.mark.asyncio
async def test_autonomy_off_stops_the_loops(live):
    import os
    ctl, started = live
    ctx, emit = _ctx(args=["off"])
    await _h_autonomy(ctx)
    (_title, text), = emit.messages
    assert os.environ["AUTONOMY_ENABLED"] == "false"
    assert "stopped in this session" in text
    assert started[0].stopped is True and ctl.handles is None


@pytest.mark.asyncio
async def test_autonomy_on_is_idempotent(live):
    ctl, started = live
    ctx, _ = _ctx(args=["on"])
    await _h_autonomy(ctx)
    ctx, emit = _ctx(args=["on"])
    await _h_autonomy(ctx)
    (_title, text), = emit.messages
    assert "already running" in text and len(started) == 2


@pytest.mark.asyncio
async def test_autonomy_on_without_a_runtime_says_restart(monkeypatch, tmp_path):
    from cli.ui import autonomy_live
    autonomy_live.bind(None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "home"))
    ctx, emit = _ctx(args=["on"])
    await _h_autonomy(ctx)
    (_title, text), = emit.messages
    assert "next start" in text
