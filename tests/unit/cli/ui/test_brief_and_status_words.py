"""113 W4 item 5 (I-3, I-4): one word, one meaning.

* "quiet" means quiet hours only: the terminal's transcript toggle is
  ``/brief``; ``/quiet`` answers for one release as its alias.
* ``/status`` is the status snapshot on every seat; ``/doctor`` is the health
  report (it adds the pointer to every check).
"""
import asyncio
import io

from core import verbs


def _run(line, monkeypatch=None):
    from cli.ui.commands import CommandContext, default_registry
    from cli.ui.plain_renderer import PlainRenderer
    from cli.ui.state import SessionState
    buf, state = io.StringIO(), SessionState()
    renderer = PlainRenderer(state=state, stream=buf)
    ctx = CommandContext(renderer=renderer, state=state, user_id="u1", session_id="s1")
    asyncio.run(default_registry().dispatch(line, ctx))
    return buf.getvalue(), renderer


def test_brief_is_the_row_and_quiet_its_old_name():
    row = verbs.verb_for("/brief")
    assert row is not None and "/quiet" in row.aliases
    assert verbs.verb_for("/quiet").name == "/brief"


def test_the_terminal_runs_brief_and_still_answers_quiet():
    from cli.ui.commands import default_registry
    reg = default_registry()
    assert reg.lookup("brief").name == "brief"
    assert reg.lookup("quiet").name == "brief"
    out, renderer = _run("/brief")
    assert "Tool transcript OFF" in out and "/brief to restore" in out
    assert renderer.show_tools is False


def test_brief_is_a_live_command():
    from cli.ui.input_policy import is_live_command
    assert is_live_command("/brief") and is_live_command("/quiet")


def test_status_is_the_snapshot_and_doctor_the_report(monkeypatch):
    monkeypatch.setattr("cli.commands.doctor.status_snapshot_lines", lambda: ["SNAP"])
    status, _ = _run("/status")
    doctor, _ = _run("/doctor")
    assert "--- status ---" in status and "SNAP" in status
    assert "polyrob doctor --full" not in status
    assert "--- doctor ---" in doctor and "polyrob doctor --full" in doctor
