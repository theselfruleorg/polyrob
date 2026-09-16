"""REPL `/doctor` (cli/ui/commands/h_diag.py::h_doctor) — 043 A13 fix round 1,
Important 3.

Before this fix, the REPL verb printed the FULL check transcript
(`doctor_report`) unconditionally, while the shell `polyrob doctor` command
(043 A13) now leads with the status snapshot by default. Two seats showing
two different things for one command name. `h_doctor` now renders through
the SAME `cli.commands.doctor.status_snapshot_lines()` + `DOCTOR_FULL_POINTER`
the CLI's default view uses — imported, not re-implemented, so the two can
never drift again.
"""
from cli.ui.commands.registry import CommandContext


def _ctx():
    emitted = []
    ctx = CommandContext()
    ctx.emit = lambda text, **k: emitted.append(text)  # type: ignore
    ctx.args = []
    return ctx, emitted


def test_h_doctor_renders_status_snapshot_lines(monkeypatch):
    from cli.ui.commands import h_diag

    monkeypatch.setattr(
        "cli.commands.doctor.status_snapshot_lines",
        lambda: ["▶ RUNNING — 0 goal run(s), 0 cron run(s), loops alive 0/0 "
                 "— `polyrob autonomy pause` to stop",
                 "Health: OK — no issue found (checked: credit sentinel)"],
    )
    ctx, emitted = _ctx()
    h_diag.h_doctor(ctx)

    body = "\n".join(emitted)
    assert "▶ RUNNING" in body
    assert "Health: OK" in body


def test_h_doctor_ends_with_the_full_pointer(monkeypatch):
    from cli.ui.commands import h_diag

    monkeypatch.setattr("cli.commands.doctor.status_snapshot_lines", lambda: ["x"])
    ctx, emitted = _ctx()
    h_diag.h_doctor(ctx)

    body = "\n".join(emitted)
    assert "run `polyrob doctor --full` for every check" in body


def test_h_doctor_does_not_call_doctor_report(monkeypatch):
    # The REPL's quick view must not pay for (or show) the full transcript.
    from cli.commands import doctor as doctor_module
    from cli.ui.commands import h_diag

    called = []
    monkeypatch.setattr(doctor_module, "doctor_report",
                        lambda *a, **k: called.append(True) or [])
    monkeypatch.setattr(doctor_module, "status_snapshot_lines", lambda: ["snap-line"])
    ctx, emitted = _ctx()
    h_diag.h_doctor(ctx)

    assert not called
    assert "snap-line" in "\n".join(emitted)


def test_h_doctor_fails_open_on_snapshot_error(monkeypatch):
    from cli.ui.commands import h_diag

    def _boom():
        raise RuntimeError("data home unreadable")

    monkeypatch.setattr("cli.commands.doctor.status_snapshot_lines", _boom)
    ctx, emitted = _ctx()
    h_diag.h_doctor(ctx)

    assert emitted
    assert "doctor unavailable" in emitted[0]


def test_h_doctor_really_renders_real_status_snapshot_lines(tmp_path, monkeypatch):
    # Not mocked — same isolation as test_doctor_snapshot_leads.py, exercising
    # the real cli.commands.doctor.status_snapshot_lines() end to end.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))

    from cli.ui.commands import h_diag

    ctx, emitted = _ctx()
    h_diag.h_doctor(ctx)

    body = "\n".join(emitted)
    assert body.strip()
    assert "run `polyrob doctor --full` for every check" in body
