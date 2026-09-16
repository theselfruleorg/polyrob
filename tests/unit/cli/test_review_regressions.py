"""Terminal review regressions: test composed workflows, not just handlers."""
import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner


@pytest.mark.asyncio
async def test_busy_editor_keeps_followup_and_dispatches_control(tmp_path, monkeypatch):
    from cli.ui.app import build_app
    from cli.ui.persistent_loop import TurnController
    from cli.ui.state import SessionState
    from prompt_toolkit.input import DummyInput
    from prompt_toolkit.output import DummyOutput
    monkeypatch.setattr("cli.ui.app.default_history_path", lambda: tmp_path / "history")
    controls, messages = [], []
    gate = asyncio.Event()
    async def turn(text):
        messages.append(text)
        await gate.wait()
    async def control(text):
        controls.append(text)
    ctrl = TurnController(run_coro_factory=turn, schedule=asyncio.create_task,
                          control_factory=control)
    app, buffer = build_app(SessionState(), on_submit=ctrl.submit,
                            output=DummyOutput(), input=DummyInput())
    buffer.text = "first"
    buffer.validate_and_handle()
    await asyncio.sleep(0)
    buffer.text = "keep my followup"
    buffer.validate_and_handle()
    assert buffer.text == "keep my followup"
    buffer.text = "/steer use another source"
    buffer.validate_and_handle()
    await asyncio.sleep(0)
    assert buffer.text == ""
    assert controls == ["/steer use another source"]
    assert messages == ["first"]
    ctrl.interrupt()
    await asyncio.gather(ctrl._task, return_exceptions=True)


@pytest.mark.asyncio
async def test_prompt_approval_uses_app_input_and_cleans_up_on_timeout():
    from cli.ui.approval_prompt import ApprovalPrompt
    from core.approval_input import approval_input
    from tools.controller.approval_interactive import InteractiveCLIApprover
    prompt = ApprovalPrompt(lambda text: None)
    token = approval_input.set(prompt.request)
    try:
        approver = InteractiveCLIApprover()
        task = asyncio.create_task(approver.request("shell_run", {}, None))
        await asyncio.sleep(0)
        assert prompt.submit("/status") is False
        assert prompt.submit("once") is True
        assert await task is True
        assert prompt.pending is None
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(approver.request("shell_run", {}, None), 0.01)
        assert prompt.pending is None
        task = asyncio.create_task(approver.request("shell_run", {}, None))
        await asyncio.sleep(0)
        assert prompt.submit("deny")
        assert await task is False
    finally:
        approval_input.reset(token)


@pytest.mark.asyncio
async def test_owner_gate_never_calls_model():
    from cli.ui.persistent_loop import run_turn
    convo = SimpleNamespace(respond=AsyncMock())
    output = []
    await run_turn(convo, "stop everything", SimpleNamespace(print_block=output.append),
                   owner_gate=lambda text: "Paused.")
    convo.respond.assert_not_called()
    assert output == ["Paused."]


@pytest.mark.asyncio
async def test_reference_preparation_reaches_persistent_conversation(monkeypatch):
    from cli.ui.persistent_loop import run_turn
    monkeypatch.setattr("cli.ui.input_policy.prepare_text", lambda text: "expanded file")
    convo = SimpleNamespace(respond=AsyncMock(return_value="ok"))
    await run_turn(convo, "@file", None)
    convo.respond.assert_awaited_once_with("expanded file")


def test_root_options_reach_explicit_chat(monkeypatch):
    from cli.polyrob import cli
    seen = []
    monkeypatch.setattr("core.profiles.activate_profile", lambda profile: None)
    monkeypatch.setattr("cli.polyrob._start_repl", lambda **kw: seen.append(kw))
    result = CliRunner().invoke(cli, ["--plain", "--model", "root-model", "chat"])
    assert result.exit_code == 0, result.output
    assert seen[0]["plain"] is True
    assert seen[0]["model"] == "root-model"


def test_resume_rejects_ignored_overrides():
    from cli.commands.run import run
    for flags in (["--max-steps", "2"], ["--tools", "filesystem"], ["--model", "other"]):
        result = CliRunner().invoke(run, ["--resume", "abc", *flags])
        assert result.exit_code == 2
        assert "remove overrides" in result.output


@pytest.mark.asyncio
async def test_empty_session_json(monkeypatch):
    from cli.commands.session import _session_list
    import contextlib
    container = SimpleNamespace(get_agent=lambda name: SimpleNamespace(
        session_manager=SimpleNamespace(get_all_sessions=lambda: [])))
    monkeypatch.setattr("cli.commands.session.cli_container", AsyncMock(return_value=container))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        await _session_list(False, True)
    assert json.loads(out.getvalue()) == []


def test_session_directory_refuses_ambiguity_and_patterns(tmp_path):
    from cli.session_paths import session_directory
    import click
    for sid in ("abcd-one", "abcd-two"):
        (tmp_path / "user" / sid).mkdir(parents=True)
    with pytest.raises(click.ClickException, match="Ambiguous"):
        session_directory(tmp_path, "abcd")
    assert session_directory(tmp_path, "abcd-one").name == "abcd-one"
    with pytest.raises(click.BadParameter):
        session_directory(tmp_path, "*")


@pytest.mark.asyncio
async def test_quoted_path_dispatch():
    from cli.ui.commands.registry import Command, CommandContext, CommandRegistry
    reg, seen = CommandRegistry(), []
    reg.register(Command("export", lambda ctx: seen.append(ctx.args)))
    await reg.dispatch('/export json "my export.json"', CommandContext())
    assert seen == [["json", "my export.json"]]


def test_literal_output_and_plain_control_filter():
    from cli.ui.rich_renderer import RichRenderer
    from cli.ui.plain_renderer import PlainRenderer
    from cli.ui.state import SessionState
    from rich.console import Console
    out = io.StringIO()
    rich = RichRenderer(SessionState(), console=Console(file=out, color_system=None))
    rich.print_block("[red]literal[/red]")
    assert "[red]literal[/red]" in out.getvalue()
    out = io.StringIO()
    plain = PlainRenderer(SessionState(), stream=out)
    plain.print_block("x\x1b[31mred\x1b[0m\x1b]0;fake title\x07")
    assert out.getvalue() == "xred\n"


def test_all_service_entrypoints_block_update():
    from cli.update.process_guard import server_process_alive
    for args in ([], ["chat"], ["gateway"], ["discord"], ["dashboard"], ["slack"]):
        assert server_process_alive(_cmdlines=[(999999, ["polyrob", *args])])
    assert not server_process_alive(_cmdlines=[(999999, ["polyrob", "session", "list"])])


def test_delegation_receipts_are_tenant_scoped(tmp_path):
    from agents.task.agent.autonomy_state import AutonomyStateStore
    from cli.delegation_receipts import read_delegations
    path = str(tmp_path / "autonomy_state.db")
    store = AutonomyStateStore(path)
    for user in ("alice", "bob"):
        store.record_dispatched(session_id=user, user_id=user, delegation_id="d1",
                                goal="research", profile="default", parent_agent_id="parent", dispatched_at=1)
    rows = read_delegations(path, "alice")
    assert len(rows) == 1 and rows[0]["user_id"] == "alice"


@pytest.mark.asyncio
async def test_attachment_staging_is_bounded_and_has_vision_blocks(tmp_path):
    from cli.attachments import stage_attachments
    source = tmp_path / "diagram.png"
    source.write_bytes(b"small image fixture")
    text, images, receipts = await stage_attachments([source], tmp_path / "workspace")
    assert "Attached image" in text
    assert images[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert Path(receipts[0]).read_bytes() == source.read_bytes()


def test_json_result_and_events_are_parseable():
    from cli.ui.json_renderer import JsonRenderer
    from cli.ui.state import SessionState
    from cli.ui.events import SessionDone
    out = io.StringIO()
    renderer = JsonRenderer(SessionState(), out, "jsonl")
    renderer.on_event(SessionDone(final_result="answer"))
    renderer.on_turn_end("answer")
    renderer.finish(0)
    records = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r["kind"] for r in records] == ["event", "result"]
    assert records[-1]["answer"] == "answer"
    assert records[-1]["success"] is True


@pytest.mark.parametrize("arguments", [["-"], ["--task-file", "-"]])
def test_stdin_run_is_noninteractive(arguments, monkeypatch):
    from cli.commands.run import run
    seen = []
    async def execute(task, *args, **kwargs):
        seen.append((task, kwargs["no_input"]))
    monkeypatch.setattr("cli.commands.run._run_session", execute)
    result = CliRunner().invoke(run, [*arguments, "--plain"], input="read this task")
    assert result.exit_code == 0, result.output
    assert seen == [("read this task", True)]


@pytest.mark.parametrize("fails", [False, True])
def test_machine_run_keeps_stdout_parseable_on_failure(fails, monkeypatch):
    from cli.commands.run import run
    async def execute(task, *args, **kwargs):
        assert kwargs["no_input"] is True
        print("bootstrap diagnostic")
        if fails:
            raise RuntimeError("provider unavailable")
        kwargs["machine"].on_turn_end("answer")
    monkeypatch.setattr("cli.commands.run._run_session", execute)
    try:
        runner = CliRunner(mix_stderr=False)
    except TypeError:  # Click 8.2+ always captures separate streams
        runner = CliRunner()
    result = runner.invoke(run, ["task", "--output-format", "json"])
    record = json.loads(result.stdout)
    assert record["success"] is (not fails)
    assert record["exit_code"] == result.exit_code == int(fails)
    assert "bootstrap diagnostic" in result.stderr


@pytest.mark.asyncio
async def test_history_survives_restart_without_credentials(tmp_path, monkeypatch):
    from cli.ui.app import build_app
    from cli.ui.state import SessionState
    from cli.ui.history import TerminalHistory
    from prompt_toolkit.input import DummyInput
    from prompt_toolkit.output import DummyOutput
    path = tmp_path / "history"
    monkeypatch.setattr("cli.ui.app.default_history_path", lambda: path)
    app, buffer = build_app(SessionState(), on_submit=lambda text: True,
                            output=DummyOutput(), input=DummyInput())
    for text in ("summarize this", "/mcp add server https://example.test private-value"):
        buffer.text = text
        buffer.validate_and_handle()
    history = TerminalHistory(str(path))
    assert list(history.load_history_strings()) == ["summarize this"]


@pytest.mark.asyncio
async def test_prose_command_preserves_apostrophes():
    from cli.ui.commands.registry import Command, CommandContext, CommandRegistry
    reg, seen = CommandRegistry(), []
    reg.register(Command("steer", lambda ctx: seen.append(ctx.raw), raw_arguments=True))
    await reg.dispatch("/steer don't change the title", CommandContext())
    assert seen == ["steer don't change the title"]
