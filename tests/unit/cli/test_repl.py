import pytest
from unittest.mock import MagicMock, AsyncMock


def _eof_reader(lines):
    it = iter(lines)
    async def read_line():
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    return read_line


@pytest.mark.asyncio
async def test_loop_runs_each_line_as_turn(capsys):
    from cli.commands.chat import _conversation_loop
    convo = MagicMock()
    convo.respond = AsyncMock(side_effect=lambda t, **k: f"reply:{t}")
    await _conversation_loop(convo, MagicMock(), read_line=_eof_reader(["first", "second"]))
    assert convo.respond.await_count == 2
    out = capsys.readouterr().out
    assert "reply:first" in out and "reply:second" in out


@pytest.mark.asyncio
async def test_start_repl_agent_returns_existing_without_building():
    from cli.commands.chat import _start_repl_agent
    orch = MagicMock()
    existing = object()
    orch.agents = {"executor_S1": existing}
    ta = MagicMock()
    ta._get_llm_for_request = AsyncMock()
    agent, preexisted = await _start_repl_agent(ta, orch, request=MagicMock(), session_id="S1")
    assert agent is existing
    assert preexisted is True
    ta._get_llm_for_request.assert_not_awaited()  # must not rebuild an existing agent


@pytest.mark.asyncio
async def test_start_repl_agent_creates_when_absent():
    from cli.commands.chat import _start_repl_agent
    orch = MagicMock()
    orch.agents = {}
    made = object()
    orch.create_agent = AsyncMock(return_value=made)
    ta = MagicMock()
    ta._get_llm_for_request = AsyncMock(return_value="LLM")
    agent, preexisted = await _start_repl_agent(ta, orch, request=MagicMock(), session_id="S1")
    assert agent is made
    assert preexisted is False
    ta._get_llm_for_request.assert_awaited_once()
    orch.create_agent.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_repl_agent_handles_failure_cleanly(capsys):
    # The most likely "REPL won't start" failure (LLM/client build) must render a
    # clean [polyrob] error and return None, NOT escape as a raw traceback.
    from cli.commands.chat import _start_repl_agent
    orch = MagicMock()
    orch.agents = {}
    ta = MagicMock()
    ta._get_llm_for_request = AsyncMock(side_effect=RuntimeError("no LLM client"))
    agent, preexisted = await _start_repl_agent(ta, orch, request=MagicMock(), session_id="S1")
    assert agent is None
    out = capsys.readouterr().out
    assert "failed to start agent" in out
    assert "no LLM client" in out
    assert "Traceback" not in out


def test_run_repl_threads_launch_flags(monkeypatch):
    # --model/--provider/--toolset launch parity with `polyrob run`.
    from cli.commands import chat as chat_mod
    captured = {}

    async def _fake_main(plain=False, lifecycle_ref=None, *, model=None,
                         provider=None, toolset=None, start_notice=None,
                         new_chat=False, chat_ref=None):
        captured.update(model=model, provider=provider, toolset=toolset,
                        new_chat=new_chat, chat_ref=chat_ref)

    monkeypatch.setattr(chat_mod, "_repl_main", _fake_main)
    chat_mod.run_repl(plain=True, model="gpt-5", provider="openai", toolset="research",
                      chat_ref="notes")
    assert captured == {"model": "gpt-5", "provider": "openai", "toolset": "research",
                        "new_chat": False, "chat_ref": "notes"}


def test_chat_command_accepts_launch_flags(monkeypatch):
    from click.testing import CliRunner
    from cli.polyrob import cli
    captured = {}

    def _fake_repl(plain=False, *, model=None, provider=None, toolset=None,
                   start_notice=None, new_chat=False, chat_ref=None):
        captured.update(model=model, provider=provider, toolset=toolset,
                        new_chat=new_chat, chat_ref=chat_ref)

    monkeypatch.setattr("cli.commands.chat.run_repl", _fake_repl)
    res = CliRunner().invoke(cli, ["chat", "-m", "gpt-5", "-p", "openai", "--toolset", "research"])
    assert res.exit_code == 0, res.output
    assert captured == {"model": "gpt-5", "provider": "openai", "toolset": "research",
                        "new_chat": False, "chat_ref": None}
    # 113: `polyrob --new` and `polyrob --chat <ref>` reach the REPL (group and `chat`)
    assert CliRunner().invoke(cli, ["--new"]).exit_code == 0 and captured["new_chat"] is True
    assert CliRunner().invoke(cli, ["chat", "--chat", "2"]).exit_code == 0
    assert captured["chat_ref"] == "2" and captured["new_chat"] is False


@pytest.mark.asyncio
async def test_blank_lines_skipped():
    from cli.commands.chat import _conversation_loop
    convo = MagicMock(); convo.respond = AsyncMock(return_value="x")
    await _conversation_loop(convo, MagicMock(), read_line=_eof_reader(["", "  ", "hi"]))
    convo.respond.assert_awaited_once_with("hi")


@pytest.mark.asyncio
async def test_slash_not_sent_as_turn():
    from cli.commands.chat import _conversation_loop
    convo = MagicMock(); convo.respond = AsyncMock(return_value="x")
    # /help is handled by the real dispatcher (prints help), 'hi' is a turn
    await _conversation_loop(convo, MagicMock(), read_line=_eof_reader(["/help", "hi"]))
    convo.respond.assert_awaited_once_with("hi")


@pytest.mark.asyncio
async def test_exit_breaks_loop():
    from cli.commands.chat import _conversation_loop
    convo = MagicMock(); convo.respond = AsyncMock(return_value="x")
    await _conversation_loop(convo, MagicMock(), read_line=_eof_reader(["/exit", "never"]))
    convo.respond.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_slash_not_sent_as_turn(capsys):
    """An unknown /command prints a hint and is NOT routed to the conversation."""
    from cli.commands.chat import _conversation_loop
    convo = MagicMock(); convo.respond = AsyncMock(return_value="x")
    await _conversation_loop(convo, MagicMock(), read_line=_eof_reader(["/bogus", "hi"]))
    convo.respond.assert_awaited_once_with("hi")
    assert "Unknown command" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_injected_slash_dispatch_used():
    """A caller-supplied slash_dispatch overrides the default registry path."""
    from cli.commands.chat import _conversation_loop
    convo = MagicMock(); convo.respond = AsyncMock(return_value="x")
    seen = {}

    async def _dispatch(line):
        seen["line"] = line
        return True  # handled → not a turn

    await _conversation_loop(
        convo, MagicMock(),
        read_line=_eof_reader(["/custom", "real"]),
        slash_dispatch=_dispatch,
    )
    assert seen["line"] == "/custom"
    convo.respond.assert_awaited_once_with("real")


@pytest.mark.asyncio
async def test_loop_routes_answer_through_renderer():
    """When a renderer is passed, on_turn_start/on_turn_end are called and
    the answer is NOT echoed directly via click (no double-print)."""
    from cli.commands.chat import _conversation_loop

    convo = MagicMock()
    convo.respond = AsyncMock(return_value="renderer-answer")

    renderer = MagicMock()
    renderer.on_turn_start = MagicMock()
    renderer.on_turn_end = MagicMock()

    await _conversation_loop(
        convo, MagicMock(),
        read_line=_eof_reader(["hello"]),
        renderer=renderer,
    )

    renderer.on_turn_start.assert_called_once_with("hello")
    renderer.on_turn_end.assert_called_once_with("renderer-answer")


@pytest.mark.asyncio
async def test_loop_no_renderer_falls_back_to_echo(capsys):
    """Without a renderer the loop still echoes the answer via click.echo."""
    from cli.commands.chat import _conversation_loop

    convo = MagicMock()
    convo.respond = AsyncMock(return_value="echo-answer")

    await _conversation_loop(
        convo, MagicMock(),
        read_line=_eof_reader(["hi"]),
        renderer=None,
    )

    out = capsys.readouterr().out
    assert "echo-answer" in out


def test_rob_no_args_invokes_repl(monkeypatch):
    called = {}
    monkeypatch.setattr(
        "cli.commands.chat.run_repl",
        lambda plain=False, **kw: called.setdefault("ran", True),
    )
    from cli.polyrob import cli
    from click.testing import CliRunner
    result = CliRunner().invoke(cli, [])
    assert called.get("ran") is True
    # subcommands still work / are listed
    assert "run" in cli.list_commands(None) and "init" in cli.list_commands(None)


@pytest.mark.asyncio
async def test_respond_exception_renders_error_and_keeps_loop():
    """A non-KeyboardInterrupt exception from respond() must render as a
    dialog-layer error (via the renderer) and continue the loop — never
    escape as a traceback that kills the REPL."""
    from io import StringIO

    from cli.commands.chat import _conversation_loop
    from cli.ui.plain_renderer import PlainRenderer
    from cli.ui.state import SessionState

    convo = MagicMock()
    convo.respond = AsyncMock(
        side_effect=[InterruptedError("Agent stopped"), "recovered reply"]
    )
    buf = StringIO()
    renderer = PlainRenderer(state=SessionState(), stream=buf)

    await _conversation_loop(
        convo,
        MagicMock(),
        read_line=_eof_reader(["first", "second"]),
        renderer=renderer,
    )

    assert convo.respond.await_count == 2  # the loop survived the first failure
    out = buf.getvalue()
    assert "error: InterruptedError: Agent stopped" in out
    assert "recovered reply" in out


@pytest.mark.asyncio
async def test_respond_exception_without_renderer_echoes(capsys):
    from cli.commands.chat import _conversation_loop

    convo = MagicMock()
    convo.respond = AsyncMock(side_effect=[RuntimeError("boom"), "ok"])
    await _conversation_loop(
        convo, MagicMock(), read_line=_eof_reader(["a", "b"])
    )
    out = capsys.readouterr().out
    assert "RuntimeError: boom" in out
    assert "ok" in out


def test_a_failed_container_start_exits_nonzero(monkeypatch):
    """113 W0 (T-06): the REPL printed "failed to start" and exited 0, so a script
    could not tell a dead start from a clean one. `polyrob run` exits 1 here."""
    import asyncio

    from cli.commands import chat as chat_mod

    async def _boom(**kw):
        raise RuntimeError("no container")
    monkeypatch.setattr("polyrob.app.bootstrap.build_cli_container", _boom)
    monkeypatch.setattr("cli.keys.preflight_or_onboard", lambda **k: True)
    monkeypatch.setattr("cli.keys.first_run_no_config", lambda: False, raising=False)
    with pytest.raises(SystemExit) as caught:
        asyncio.run(chat_mod._repl_main(plain=True))
    assert caught.value.code == 1


def test_kb_export_defaults_to_the_owner_tenant(monkeypatch):
    """113 W0 (T-07): `--user` defaulted to "local", an empty tenant on a bound install."""
    from cli.commands import knowledge as kn
    seen = {}

    async def _provider():
        return None, "/tmp/x"

    async def _vault(out_dir, **kw):
        seen.update(kw)
        return {}
    monkeypatch.setattr(kn, "_bootstrap", lambda: None)
    monkeypatch.setattr(kn, "_resolve_provider_and_data_dir", _provider)
    monkeypatch.setattr("agents.task.knowledge_export.build_vault", _vault)
    monkeypatch.setattr("core.admin_data_home.admin_owner_principal", lambda: "u_owner")
    try:
        kn.run_export("/tmp/vault-x", None, None)
    except Exception:
        pass   # the summary print of an empty manifest is not the subject
    assert seen["user_id"] == "u_owner"
