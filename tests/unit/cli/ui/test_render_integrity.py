"""Rendering/accounting boundaries; no providers or external services needed."""
import io
import json

import pytest

from cli.ui.events import AgentRegistration, ErrorEvent, Info, SessionDone, SessionStart, Step
from cli.ui.plain_renderer import PlainRenderer
from cli.ui.state import SessionState


def renderer():
    state = SessionState()
    state.update(AgentRegistration(agent_id='main', agent_name='parent'))
    return PlainRenderer(state, stream=io.StringIO())


def test_long_turn_metrics_and_failure_survive_trace_limit():
    r = renderer()
    r.on_turn_start('work')
    for i in range(520):
        r.on_event(Step(step=i, actions=[{'action_type': 'read_file'}, {'action_type': 'done'}]))
    r.on_event(ErrorEvent(error_message='late failure'))
    assert r.turn_steps() == 520
    assert r.turn_tool_calls() == 520
    assert r.turn_failed()
    assert len(r._turn_events) == 500
    assert isinstance(r._turn_events[-1], ErrorEvent)
    r.on_turn_start('next')
    assert r.turn_steps() == r.turn_tool_calls() == 0
    assert not r.turn_failed()


def test_child_message_is_not_main_dialog_or_main_step():
    r = renderer()
    r.on_turn_start('delegate')
    child = Step(step=1, raw={'data': {'agent_id': 'child'}}, actions=[
        {'action_type': 'send_message', 'params': {'text': 'private child reply'}}])
    r.on_event(child)
    assert 'private child reply' not in r._stream.getvalue()
    assert r.turn_steps() == 0
    r.state.update(SessionStart(agent_id='child', model_name='child-model'))
    assert r.state.model != 'child-model'


def test_usage_partial_write_is_retried_exactly_once(tmp_path):
    usage = tmp_path / 'data' / 'llm_usage'
    usage.mkdir(parents=True)
    path = usage / 'llm_usage_1.json'
    path.write_text('{"prompt_tokens":')
    s = SessionState()
    s.poll_usage(tmp_path)
    path.write_text(json.dumps({'prompt_tokens': 20, 'token_count': 23, 'cost_estimate': .01}))
    s.poll_usage(tmp_path)
    s.poll_usage(tmp_path)
    assert s.tokens_in == 20
    assert s.tokens_total == 23
    assert s.cost_estimate_total == .01


def test_bad_usage_record_does_not_starve_following_records(tmp_path):
    usage = tmp_path / 'data' / 'llm_usage'
    usage.mkdir(parents=True)
    (usage / 'llm_usage_1.json').write_text('[]')
    (usage / 'llm_usage_2.json').write_text('{"token_count": 5, "prompt_tokens": NaN}')
    s = SessionState()
    s.poll_usage(tmp_path)
    s.poll_usage(tmp_path)
    assert s.tokens_total == 5
    assert s.tokens_in == 0


def test_new_turn_clears_previous_activity():
    r = renderer()
    r.state.last_tool = 'old-tool'
    r.state._set_activity('approval', 'old approval')
    r.on_turn_start('new')
    assert not r.state.last_tool
    assert not r.state.current_activity


def test_session_elapsed_uses_injected_clock():
    now = [100.0]
    s = SessionState(clock=lambda: now[0])
    now[0] += 3
    assert s.elapsed() == 3


@pytest.mark.asyncio
async def test_failed_feed_settles_toolbar_as_error():
    from cli.ui.persistent_loop import run_turn
    r = renderer()
    class Conversation:
        async def respond(self, text):
            r.on_event(SessionDone(success=False, error_message='provider refused'))
            return ''
    await run_turn(Conversation(), 'hello', r)
    assert r.state.status == 'error'


def test_plain_stdout_follows_prompt_proxy(monkeypatch):
    import sys
    original, proxy = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, 'stdout', original)
    r = PlainRenderer(SessionState(), stream=sys.stdout)
    monkeypatch.setattr(sys, 'stdout', proxy)
    r.print_block('above prompt')
    assert original.getvalue() == ''
    assert proxy.getvalue() == 'above prompt\n'


def test_unknown_cost_is_not_reported_as_free():
    from cli.ui.events import LLMCall
    from cli.ui.statusbar import status_text
    r = renderer()
    r.on_turn_start('work')
    r.state.update(LLMCall(token_count=10, cost_estimate=None))
    assert 'cost unknown' in status_text(r.state)
    assert r.turn_cost_incomplete()
    r.on_turn_start('next')
    assert not r.turn_cost_incomplete()


def test_narrow_status_keeps_lifecycle_visible():
    from cli.ui.statusbar import status_formatted
    from prompt_toolkit.utils import get_cwidth
    s = SessionState()
    s.model = 'long-model-name' * 10
    s.status = 'error'
    text = ''.join(text for _, text in status_formatted(s, width=20))
    assert text.startswith('error')
    assert text.endswith('…')
    assert get_cwidth(text) <= 20


def test_single_row_fits_wide_unicode_and_newlines():
    from cli.ui.line_layout import fit_fragments
    from prompt_toolkit.utils import get_cwidth
    for width in range(15):
        result = ''.join(text for _, text in fit_fragments([('', '你好\nmodel e\u0301 text')], width))
        assert '\n' not in result
        assert get_cwidth(result) <= width


def test_autonomy_read_failure_is_visible():
    from cli.ui.statusbar import autonomy_line
    s = SessionState()
    s.autonomy_snapshot = {'goals': None, 'cron': 0, 'review': False}
    assert 'goals unknown' in autonomy_line(s)


def test_dumb_or_redirected_terminal_has_no_cursor_ui(monkeypatch):
    import sys
    from cli.ui.theme import supports_cursor_ui
    class TTY(io.StringIO):
        def isatty(self): return True
    monkeypatch.setattr(sys, 'stdin', TTY())
    monkeypatch.setattr(sys, 'stdout', TTY())
    monkeypatch.setenv('TERM', 'xterm-256color')
    assert supports_cursor_ui()
    monkeypatch.setenv('TERM', 'dumb')
    assert not supports_cursor_ui()
    monkeypatch.setenv('TERM', 'xterm-256color')
    monkeypatch.setattr(sys, 'stdout', io.StringIO())
    assert not supports_cursor_ui()


@pytest.mark.asyncio
async def test_wrapped_input_preferred_height_grows(monkeypatch, tmp_path):
    from cli.ui.app import build_app
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from prompt_toolkit.layout.controls import BufferControl
    monkeypatch.setenv('POLYROB_HOME', str(tmp_path))
    with create_pipe_input() as pipe:
        app, buf = build_app(SessionState(), on_submit=lambda _: None, input=pipe, output=DummyOutput())
        win = next(w for w in app.layout.find_all_windows() if isinstance(w.content, BufferControl))
        assert win.preferred_height(40, 24).preferred == 1
        buf.text = 'long input ' * 14
        assert 1 < win.preferred_height(40, 24).preferred <= 10
        buf.text = '你好' * 30
        assert win.preferred_height(40, 24).preferred > 1


def test_terminal_success_can_confirm_recovery():
    r = renderer()
    r.on_turn_start('recover')
    r.on_event(ErrorEvent(error_message='temporary error'))
    assert r.turn_failed()
    r.on_event(SessionDone(success=True))
    assert not r.turn_failed()


def test_verbose_tool_name_is_literal_rich_text():
    from cli.ui.rich_renderer import RichRenderer
    from cli.ui.events import ToolStarted
    from rich.console import Console
    stream = io.StringIO()
    r = RichRenderer(SessionState(), console=Console(file=stream, color_system=None))
    r.verbose = True
    r.on_event(ToolStarted(tool_name='tool', action_name='[bold]literal[/bold]'))
    assert '[bold]literal[/bold]' in stream.getvalue()
