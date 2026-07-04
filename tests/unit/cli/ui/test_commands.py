"""Tests for the Phase 4 slash-command registry + handlers (cli/ui/commands.py)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from cli.ui.commands import (
    Command,
    CommandContext,
    CommandRegistry,
    ReplExit,
    SlashCompleter,
    build_completer,
    build_default_registry,
    default_registry,
)
from cli.ui.plain_renderer import PlainRenderer
from cli.ui.state import SessionState


# ---------------------------------------------------------------------------
# Stub context helpers
# ---------------------------------------------------------------------------


class _StubTurn:
    def __init__(self, user: str, assistant: str = "") -> None:
        self.user = user
        self.assistant = assistant


def _plain_ctx(**overrides):
    """Build a CommandContext with a PlainRenderer writing to a StringIO."""
    buf = io.StringIO()
    state = overrides.pop("state", SessionState())
    renderer = PlainRenderer(state=state, stream=buf)
    ctx = CommandContext(renderer=renderer, state=state, **overrides)
    return ctx, buf


# ---------------------------------------------------------------------------
# Registry: registration / alias / lookup / unknown
# ---------------------------------------------------------------------------


def test_register_and_lookup():
    reg = CommandRegistry()
    cmd = Command("foo", lambda c: None, "do foo", aliases=("f",))
    reg.register(cmd)
    assert reg.lookup("foo") is cmd
    assert reg.lookup("/foo") is cmd
    assert reg.lookup("FOO") is cmd
    assert reg.lookup("f") is cmd  # alias
    assert reg.lookup("nope") is None
    assert "foo" in reg
    assert "/f" in reg


def test_register_duplicate_name_raises():
    reg = CommandRegistry()
    reg.register(Command("foo", lambda c: None))
    with pytest.raises(ValueError):
        reg.register(Command("foo", lambda c: None))


def test_register_duplicate_alias_raises():
    reg = CommandRegistry()
    reg.register(Command("foo", lambda c: None, aliases=("x",)))
    with pytest.raises(ValueError):
        reg.register(Command("bar", lambda c: None, aliases=("x",)))


def test_names_includes_aliases():
    reg = CommandRegistry()
    reg.register(Command("foo", lambda c: None, aliases=("f", "ff")))
    assert set(reg.names()) >= {"foo", "f", "ff"}


@pytest.mark.asyncio
async def test_dispatch_non_slash_returns_false():
    reg = CommandRegistry()
    ctx = CommandContext()
    assert await reg.dispatch("hello world", ctx) is False


@pytest.mark.asyncio
async def test_dispatch_unknown_is_handled_with_hint():
    reg = build_default_registry()
    ctx, buf = _plain_ctx()
    assert await reg.dispatch("/bogus", ctx) is True
    assert "Unknown command" in buf.getvalue()


@pytest.mark.asyncio
async def test_dispatch_routes_args():
    captured = {}

    def handler(c: CommandContext) -> None:
        captured["args"] = c.args

    reg = CommandRegistry()
    reg.register(Command("echo", handler))
    ctx = CommandContext()
    await reg.dispatch("/echo a b c", ctx)
    assert captured["args"] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_dispatch_awaits_async_handler():
    seen = {}

    async def handler(c: CommandContext) -> None:
        seen["ran"] = True

    reg = CommandRegistry()
    reg.register(Command("a", handler))
    await reg.dispatch("/a", CommandContext())
    assert seen.get("ran") is True


# ---------------------------------------------------------------------------
# Completer
# ---------------------------------------------------------------------------


class _Doc:
    def __init__(self, text: str) -> None:
        self.text_before_cursor = text


def _completions(completer, text):
    return [c.text for c in completer.get_completions(_Doc(text), None)]


def test_completer_completes_command_names():
    reg = build_default_registry()
    completer = SlashCompleter(reg)
    out = _completions(completer, "/st")
    assert "status" in out


def test_completer_no_completion_without_slash():
    reg = build_default_registry()
    completer = SlashCompleter(reg)
    assert _completions(completer, "hello") == []


def test_completer_prefix_filters():
    reg = build_default_registry()
    completer = SlashCompleter(reg)
    out = _completions(completer, "/us")
    assert "usage" in out
    assert "status" not in out


def test_completer_resume_session_ids():
    reg = build_default_registry()
    completer = SlashCompleter(reg, sessions_provider=lambda: ["abc123", "abd999", "zzz"])
    out = _completions(completer, "/resume ab")
    assert set(out) == {"abc123", "abd999"}


def test_build_completer_is_pt_completer():
    from prompt_toolkit.completion import Completer

    reg = build_default_registry()
    c = build_completer(reg)
    assert isinstance(c, Completer)
    out = [comp.text for comp in c.get_completions(_Doc("/he"), None)]
    assert "help" in out


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_help_lists_commands():
    reg = default_registry()
    ctx, buf = _plain_ctx()
    await reg.dispatch("/help", ctx)
    out = buf.getvalue()
    assert "/status" in out and "/usage" in out and "/resume" in out


@pytest.mark.asyncio
async def test_help_uses_custom_registry():
    """A custom registry passed via ctx.registry shows its extra command in /help."""
    from cli.ui.commands import Command, CommandRegistry, CommandContext

    custom_reg = CommandRegistry()
    custom_reg.register(Command("help", lambda c: None, "Show help"))
    custom_reg.register(Command("myspecialcmd", lambda c: None, "A custom command"))

    ctx, buf = _plain_ctx()
    ctx.registry = custom_reg
    # Dispatch /help through the custom registry (it has a help handler that calls _h_help
    # indirectly — but we call _h_help directly here to test the ctx.registry path).
    from cli.ui.commands import _h_help
    _h_help(ctx)
    out = buf.getvalue()
    assert "myspecialcmd" in out
    # The default-registry-only commands should NOT appear (we used the custom registry).
    assert "resume" not in out


@pytest.mark.asyncio
async def test_exit_raises_replexit():
    reg = default_registry()
    ctx, _ = _plain_ctx()
    with pytest.raises(ReplExit):
        await reg.dispatch("/exit", ctx)
    # alias
    with pytest.raises(ReplExit):
        await reg.dispatch("/quit", ctx)


@pytest.mark.asyncio
async def test_status_plain():
    reg = default_registry()
    state = SessionState()
    state.model = "gemini-2.5-flash"
    state.provider = "gemini"
    state.tokens_in = 100
    state.tokens_out = 50
    state.step = 3
    ctx, buf = _plain_ctx(state=state, session_id="abcd1234efgh")
    await reg.dispatch("/status", ctx)
    out = buf.getvalue()
    assert "gemini-2.5-flash" in out
    assert "100 in" in out
    assert "step: 3" in out


@pytest.mark.asyncio
async def test_tools_grouped():
    class _Action:
        def __init__(self, tool):
            self.tool = tool

    actions = {
        "read_file": _Action("filesystem"),
        "write_file": _Action("filesystem"),
        "send_message": _Action("send"),
    }

    class _Registry:
        pass

    agent = type("A", (), {})()
    inner = _Registry()
    inner.registry = _Registry()
    inner.registry.actions = actions
    agent.controller = type("C", (), {})()
    agent.controller.registry = inner

    convo = type("Conv", (), {})()
    convo.agent = agent

    reg = default_registry()
    ctx, buf = _plain_ctx(conversation=convo)
    await reg.dispatch("/tools", ctx)
    out = buf.getvalue()
    assert "filesystem" in out
    assert "read_file" in out and "write_file" in out
    assert "send" in out


@pytest.mark.asyncio
async def test_tools_missing_registry():
    convo = type("Conv", (), {})()
    convo.agent = type("A", (), {})()
    reg = default_registry()
    ctx, buf = _plain_ctx(conversation=convo)
    await reg.dispatch("/tools", ctx)
    assert "no registered tools" in buf.getvalue()


@pytest.mark.asyncio
async def test_sessions_table():
    class _SM:
        def get_all_sessions(self):
            return [
                {"id": "sess-1", "status": "created", "created_at": "2026-06-11T10:00:00",
                 "agents": [{"model": "gemini-2.5-flash"}]},
                {"id": "sess-2", "status": "completed", "created_at": "2026-06-11T11:00:00"},
            ]

    task_agent = type("TA", (), {})()
    task_agent.session_manager = _SM()
    reg = default_registry()
    ctx, buf = _plain_ctx(task_agent=task_agent)
    await reg.dispatch("/sessions", ctx)
    out = buf.getvalue()
    assert "sess-1" in out and "sess-2" in out
    assert "gemini-2.5-flash" in out


@pytest.mark.asyncio
async def test_sessions_empty():
    class _SM:
        def get_all_sessions(self):
            return []

    task_agent = type("TA", (), {})()
    task_agent.session_manager = _SM()
    reg = default_registry()
    ctx, buf = _plain_ctx(task_agent=task_agent)
    await reg.dispatch("/sessions", ctx)
    assert "No sessions" in buf.getvalue()


@pytest.mark.asyncio
async def test_history():
    convo = type("Conv", (), {})()
    convo.agent = None
    convo.turns = [_StubTurn("hi", "hello"), _StubTurn("more?", "yes")]
    reg = default_registry()
    ctx, buf = _plain_ctx(conversation=convo)
    await reg.dispatch("/history", ctx)
    out = buf.getvalue()
    assert "hi" in out and "hello" in out and "more?" in out


@pytest.mark.asyncio
async def test_history_empty():
    convo = type("Conv", (), {})()
    convo.turns = []
    reg = default_registry()
    ctx, buf = _plain_ctx(conversation=convo)
    await reg.dispatch("/history", ctx)
    assert "No conversation history" in buf.getvalue()


@pytest.mark.asyncio
async def test_clear_calls_clear_history_keep_system():
    called = {}

    class _MM:
        def clear_history_keep_system(self, keep_last_n=2):
            called["keep"] = keep_last_n

    agent = type("A", (), {})()
    agent.message_manager = _MM()
    convo = type("Conv", (), {})()
    convo.agent = agent
    convo.turns = [_StubTurn("x")]
    reg = default_registry()
    ctx, buf = _plain_ctx(conversation=convo)
    await reg.dispatch("/clear", ctx)
    assert called["keep"] == 2
    assert convo.turns == []
    assert "History cleared" in buf.getvalue()


@pytest.mark.asyncio
async def test_verbose_toggles_renderer():
    reg = default_registry()
    ctx, buf = _plain_ctx()
    assert ctx.renderer.verbose is False
    await reg.dispatch("/verbose", ctx)
    assert ctx.renderer.verbose is True
    assert "ON" in buf.getvalue()
    await reg.dispatch("/verbose", ctx)
    assert ctx.renderer.verbose is False


@pytest.mark.asyncio
async def test_quiet_toggles_show_tools():
    reg = default_registry()
    ctx, buf = _plain_ctx()
    # Tool transcript is ON by default; /quiet mutes it.
    assert ctx.renderer.show_tools is True
    await reg.dispatch("/quiet", ctx)
    assert ctx.renderer.show_tools is False
    assert "OFF" in buf.getvalue() or "muted" in buf.getvalue().lower()
    await reg.dispatch("/quiet", ctx)
    assert ctx.renderer.show_tools is True


@pytest.mark.asyncio
async def test_model_persists(monkeypatch):
    called = {}
    monkeypatch.setattr(
        "cli.config_store.set_default_model",
        lambda p, m: called.update(provider=p, model=m),
    )
    reg = default_registry()
    ctx, buf = _plain_ctx()
    await reg.dispatch("/model anthropic claude-opus-4-8", ctx)
    assert called == {"provider": "anthropic", "model": "claude-opus-4-8"}
    assert "new sessions" in buf.getvalue().lower()


@pytest.mark.asyncio
async def test_model_slash_form(monkeypatch):
    called = {}
    monkeypatch.setattr(
        "cli.config_store.set_default_model",
        lambda p, m: called.update(provider=p, model=m),
    )
    reg = default_registry()
    ctx, buf = _plain_ctx()
    await reg.dispatch("/model openai/gpt-4o", ctx)
    assert called == {"provider": "openai", "model": "gpt-4o"}


class _FakePM:
    """Minimal pm() stand-in that routes session paths under a tmp dir."""
    def __init__(self, root):
        self._root = root

    def get_todo_file_path(self, session_id, user_id=None):
        return self._root / "todo.md"

    def get_logs_dir(self, session_id, user_id=None):
        return self._root / "logs"

    def get_session_root(self, session_id, user_id=None):
        return self._root


def test_todos_reads_session_file_via_pm(monkeypatch, tmp_path):
    # /todos must read the agent's session todo file (pm), NOT ./todo.md in CWD.
    (tmp_path / "todo.md").write_text("- [ ] alpha\n- [x] beta\n")
    monkeypatch.setattr("agents.task.path.pm", lambda: _FakePM(tmp_path))
    from cli.ui.commands.handlers import _h_todos
    ctx, buf = _plain_ctx(session_id="s1", user_id="u1")
    _h_todos(ctx)
    out = buf.getvalue()
    assert "alpha" in out
    assert "1/2" in out  # one of two completed


def test_logs_finds_dir_via_pm(monkeypatch, tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "run.log").write_text("hello\n")
    monkeypatch.setattr("agents.task.path.pm", lambda: _FakePM(tmp_path))
    from cli.ui.commands.handlers import _h_logs
    ctx, buf = _plain_ctx(session_id="s1", user_id="u1")
    _h_logs(ctx)
    out = buf.getvalue()
    assert "run.log" in out
    assert "No logs found" not in out


def test_export_includes_conversation_turns(monkeypatch, tmp_path):
    import json as _json
    from types import SimpleNamespace
    monkeypatch.setattr("agents.task.path.pm", lambda: _FakePM(tmp_path))
    monkeypatch.chdir(tmp_path)
    convo = SimpleNamespace(
        turns=[SimpleNamespace(user="hi", assistant="hello there")],
        agent=None,
    )
    from cli.ui.commands.handlers import _h_export
    ctx, buf = _plain_ctx(session_id="sess12345678", user_id="u1", conversation=convo, args=["json"])
    _h_export(ctx)
    out_file = tmp_path / "sess12345678_export.json"
    assert out_file.exists(), buf.getvalue()
    data = _json.loads(out_file.read_text())
    assert data["turns"] == [{"user": "hi", "assistant": "hello there"}]


def test_clear_resets_live_counters_but_keeps_cost():
    from types import SimpleNamespace
    from cli.ui.commands.handlers import _h_clear
    ctx, buf = _plain_ctx()
    ctx.state.step = 5
    ctx.state.ctx_percent = 80.0
    ctx.state.cost_estimate_total = 1.23  # cumulative spend — must be preserved
    mm = SimpleNamespace(clear_history_keep_system=lambda: None)
    ctx.conversation = SimpleNamespace(agent=SimpleNamespace(message_manager=mm), turns=[1, 2])
    _h_clear(ctx)
    assert ctx.state.step == 0
    assert ctx.state.ctx_percent == 0.0
    assert ctx.state.cost_estimate_total == 1.23


@pytest.mark.asyncio
async def test_compact_emits_progress_notice():
    from types import SimpleNamespace
    from cli.ui.commands.handlers import _h_compact

    async def _compact():
        return True

    ctx, buf = _plain_ctx()
    mm = SimpleNamespace(llm_compact_history=_compact)
    ctx.conversation = SimpleNamespace(agent=SimpleNamespace(message_manager=mm), turns=[])
    await _h_compact(ctx)
    assert "compacting" in buf.getvalue().lower()


def test_replay_command_registered_with_resume_alias():
    reg = default_registry()
    names = {c.name for c in reg.commands()}
    assert "replay" in names
    resolved = reg.lookup("resume")
    assert resolved is not None and resolved.name == "replay"  # /resume is a back-compat alias


@pytest.mark.asyncio
async def test_model_rejects_unknown_provider(monkeypatch):
    # /model with an unknown provider must NOT persist (resolve_runtime_config would
    # silently drop it next launch) — matches `polyrob model set-default`.
    called = {}
    monkeypatch.setattr(
        "cli.config_store.set_default_model",
        lambda p, m: called.update(provider=p, model=m),
    )
    reg = default_registry()
    ctx, buf = _plain_ctx()
    await reg.dispatch("/model notaprovider somemodel", ctx)
    assert not called  # not persisted
    assert "unknown provider" in buf.getvalue().lower()


@pytest.mark.asyncio
async def test_model_usage_when_single_ambiguous_arg():
    # A lone arg with no "/" is still ambiguous (not a provider+model pair, not a
    # "provider/model" string) — Usage is still shown for THIS shape. (0 args now
    # launches the picker instead — see test_model_no_args_launches_picker_and_persists.)
    reg = default_registry()
    ctx, buf = _plain_ctx()
    await reg.dispatch("/model openai", ctx)
    assert "Usage" in buf.getvalue()


@pytest.mark.asyncio
async def test_model_no_args_launches_picker_and_persists(monkeypatch):
    called = {}
    monkeypatch.setattr(
        "cli.config_store.set_default_model",
        lambda p, m: called.update(provider=p, model=m),
    )
    async def _fake(*a, **k):
        return ("openrouter", "z-ai/glm-5.2")
    monkeypatch.setattr("cli.ui.model_selector.run_standalone_async", _fake)
    reg = default_registry()
    ctx, buf = _plain_ctx()
    await reg.dispatch("/model", ctx)
    assert called == {"provider": "openrouter", "model": "z-ai/glm-5.2"}
    assert "new sessions" in buf.getvalue().lower()  # same persistence message as the 2-arg form


@pytest.mark.asyncio
async def test_model_no_args_picker_cancel_is_clean(monkeypatch):
    # No persistent app running in _plain_ctx → /model falls to the standalone
    # arrow-key selector; a None return (cancel/Esc) must not persist anything.
    called = {}
    monkeypatch.setattr(
        "cli.config_store.set_default_model",
        lambda p, m: called.update(provider=p, model=m),
    )
    async def _fake_none(*a, **k):
        return None
    monkeypatch.setattr("cli.ui.model_selector.run_standalone_async", _fake_none)
    reg = default_registry()
    ctx, buf = _plain_ctx()
    await reg.dispatch("/model", ctx)
    assert not called  # not persisted
    assert "cancel" in buf.getvalue().lower()


@pytest.mark.asyncio
async def test_model_picker_uses_embedded_picker_when_app_running(monkeypatch):
    """Under the persistent REPL the picker is the app-embedded arrow-key selector
    (app._picker.open), NOT the old run_in_terminal(input()) path that swallowed the
    menu. Assert /model awaits picker.open and persists its result."""
    used = {}

    class _FakePicker:
        async def open(self, choices, default_idx, notes):
            used["open"] = True
            return ("openrouter", "z-ai/glm-5.2")

    class _FakeApp:
        is_running = True
        _picker = _FakePicker()

    from modules.llm.available_models import ModelChoice
    monkeypatch.setattr("prompt_toolkit.application.current.get_app_or_none",
                        lambda: _FakeApp())
    # The handler imports available_models/steer_notes from their home module.
    monkeypatch.setattr("modules.llm.available_models.available_models",
                        lambda env=None, **k: [ModelChoice(
                            provider="openrouter", model="z-ai/glm-5.2", display_name="GLM 5.2",
                            is_default=True, context_window=128000, pricing_hint="",
                            supports_vision=False, supports_tools=True)])
    monkeypatch.setattr("modules.llm.available_models.steer_notes", lambda env=None: [])
    monkeypatch.setattr("cli.ui.model_selector._resolved_default", lambda env, choices: None)
    persisted = {}
    monkeypatch.setattr("cli.config_store.set_default_model",
                        lambda p, m: persisted.update(provider=p, model=m))
    reg = default_registry()
    ctx, buf = _plain_ctx()
    await reg.dispatch("/model", ctx)
    assert used.get("open") is True
    assert persisted == {"provider": "openrouter", "model": "z-ai/glm-5.2"}


@pytest.mark.asyncio
async def test_model_picker_preselect_is_live_model(monkeypatch):
    """The picker's default preselect reflects the model the agent is actually running.
    With no app running, /model uses the standalone selector; capture its preselect."""
    seen = {}
    monkeypatch.setattr("cli.config_store.set_default_model", lambda p, m: None)
    async def _cap(*a, **k):
        seen.update(preselect=k.get("preselect"))
        return None
    monkeypatch.setattr("cli.ui.model_selector.run_standalone_async", _cap)
    agent = type("A", (), {"model_name": "z-ai/glm-5.2", "llm_provider": "openrouter"})()
    convo = type("Conv", (), {})()
    convo.agent = agent
    reg = default_registry()
    ctx, buf = _plain_ctx(conversation=convo)
    await reg.dispatch("/model", ctx)
    assert seen.get("preselect") == ("openrouter", "z-ai/glm-5.2")


# ---------------------------------------------------------------------------
# /model — live swap (B2)
# ---------------------------------------------------------------------------


class _FakeSwapAgent:
    """Stub agent exposing async swap_model(), like the real Agent (B1)."""

    def __init__(self, result):
        self._result = result
        self.calls = []

    async def swap_model(self, provider, model):
        self.calls.append((provider, model))
        return self._result


@pytest.mark.asyncio
async def test_model_live_swap_success(monkeypatch):
    persisted = {}
    monkeypatch.setattr(
        "cli.config_store.set_default_model",
        lambda p, m: persisted.update(provider=p, model=m),
    )
    agent = _FakeSwapAgent({"ok": True, "provider": "anthropic", "model": "claude-sonnet-4-5", "previous": {}})
    convo = type("Conv", (), {})()
    convo.agent = agent
    reg = default_registry()
    ctx, buf = _plain_ctx(conversation=convo)
    await reg.dispatch("/model anthropic claude-sonnet-4-5", ctx)
    assert agent.calls == [("anthropic", "claude-sonnet-4-5")]
    assert persisted == {"provider": "anthropic", "model": "claude-sonnet-4-5"}
    out = buf.getvalue().lower()
    assert "swapped live" in out or "live swap" in out


@pytest.mark.asyncio
async def test_model_live_swap_repaints_frame(monkeypatch):
    """After a live swap the toolbar/frame must show the NEW model immediately —
    SessionState.model is only set from an LLMCall event when unset, so the handler
    must push it (else the bar keeps the pre-swap model)."""
    from cli.ui.state import SessionState
    monkeypatch.setattr("cli.config_store.set_default_model", lambda p, m: None)
    agent = _FakeSwapAgent({"ok": True, "provider": "anthropic", "model": "claude-sonnet-4-5", "previous": {}})
    convo = type("Conv", (), {})()
    convo.agent = agent
    state = SessionState()
    state.model = "z-ai/glm-5.2"
    state.provider = "openrouter"
    reg = default_registry()
    ctx, buf = _plain_ctx(conversation=convo, state=state)
    await reg.dispatch("/model anthropic claude-sonnet-4-5", ctx)
    assert state.model == "claude-sonnet-4-5"
    assert state.provider == "anthropic"


@pytest.mark.asyncio
async def test_model_live_swap_failure_still_persists(monkeypatch):
    persisted = {}
    monkeypatch.setattr(
        "cli.config_store.set_default_model",
        lambda p, m: persisted.update(provider=p, model=m),
    )
    agent = _FakeSwapAgent({"ok": False, "error": "boom", "previous": {}})
    convo = type("Conv", (), {})()
    convo.agent = agent
    reg = default_registry()
    ctx, buf = _plain_ctx(conversation=convo)
    await reg.dispatch("/model anthropic claude-sonnet-4-5", ctx)
    assert agent.calls == [("anthropic", "claude-sonnet-4-5")]
    assert persisted == {"provider": "anthropic", "model": "claude-sonnet-4-5"}
    out = buf.getvalue().lower()
    assert "saved" in out or "default" in out
    assert "failed" in out
    assert "boom" in out


@pytest.mark.asyncio
async def test_model_no_live_agent_falls_back_to_persist_only(monkeypatch):
    persisted = {}
    monkeypatch.setattr(
        "cli.config_store.set_default_model",
        lambda p, m: persisted.update(provider=p, model=m),
    )
    reg = default_registry()
    ctx, buf = _plain_ctx()  # no conversation set -> ctx.agent is None
    await reg.dispatch("/model anthropic claude-sonnet-4-5", ctx)
    assert persisted == {"provider": "anthropic", "model": "claude-sonnet-4-5"}
    out = buf.getvalue().lower()
    assert "new sessions" in out
    assert "no live session" in out


# ---------------------------------------------------------------------------
# /model <alias> — B6 model_aliases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_model_alias_expands_and_swaps(monkeypatch):
    persisted = {}
    monkeypatch.setattr(
        "cli.config_store.set_default_model",
        lambda p, m: persisted.update(provider=p, model=m),
    )
    monkeypatch.setattr(
        "cli.config_store.load_cli_config",
        lambda: {"model_aliases": {"fav": "anthropic/claude-sonnet-4-5"}},
    )
    agent = _FakeSwapAgent({"ok": True, "provider": "anthropic", "model": "claude-sonnet-4-5", "previous": {}})
    convo = type("Conv", (), {})()
    convo.agent = agent
    reg = default_registry()
    ctx, buf = _plain_ctx(conversation=convo)
    await reg.dispatch("/model fav", ctx)
    assert agent.calls == [("anthropic", "claude-sonnet-4-5")]
    assert persisted == {"provider": "anthropic", "model": "claude-sonnet-4-5"}
    out = buf.getvalue().lower()
    assert "swapped live" in out or "live swap" in out


@pytest.mark.asyncio
async def test_model_alias_bare_model_infers_provider(monkeypatch):
    persisted = {}
    monkeypatch.setattr(
        "cli.config_store.set_default_model",
        lambda p, m: persisted.update(provider=p, model=m),
    )
    monkeypatch.setattr(
        "cli.config_store.load_cli_config",
        lambda: {"model_aliases": {"mini": "gpt-5"}},
    )
    reg = default_registry()
    ctx, buf = _plain_ctx()  # no live agent -> persist-only path
    await reg.dispatch("/model mini", ctx)
    assert persisted == {"provider": "openai", "model": "gpt-5"}


@pytest.mark.asyncio
async def test_model_unknown_alias_is_clean_error(monkeypatch):
    persisted = {}
    monkeypatch.setattr(
        "cli.config_store.set_default_model",
        lambda p, m: persisted.update(provider=p, model=m),
    )
    monkeypatch.setattr("cli.config_store.load_cli_config", lambda: {})
    reg = default_registry()
    ctx, buf = _plain_ctx()
    await reg.dispatch("/model nope", ctx)
    assert not persisted
    out = buf.getvalue().lower()
    assert "unknown alias" in out
    assert "usage" in out


# ---------------------------------------------------------------------------
# /compact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compact_awaits_async_call():
    called = {}

    class _MM:
        async def llm_compact_history(self):
            called["ran"] = True
            return True

    agent = type("A", (), {})()
    agent.message_manager = _MM()
    convo = type("Conv", (), {})()
    convo.agent = agent
    reg = default_registry()
    ctx, buf = _plain_ctx(conversation=convo)
    await reg.dispatch("/compact", ctx)
    assert called.get("ran") is True
    assert "compacted" in buf.getvalue().lower()


# ---------------------------------------------------------------------------
# /usage — DB breakdown + fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_db_breakdown():
    breakdown = {
        "session_id": "s1",
        "total_credits_charged": 12,
        "total_api_cost_usd": 0.0123,
        "total_markup_usd": 0.001,
        "by_type": [
            {
                "type": "llm",
                "calls": 3,
                "tokens": {"input": 100, "output": 40, "cached": 10},
                "credits_charged": 12,
                "api_cost_usd": 0.0123,
                "markup_usd": 0.001,
            }
        ],
    }

    class _Tracker:
        async def get_session_breakdown(self, sid):
            return breakdown

    orchestrator = type("O", (), {})()
    orchestrator.usage_tracker = _Tracker()
    reg = default_registry()
    ctx, buf = _plain_ctx(session_id="s1", orchestrator=orchestrator)
    await reg.dispatch("/usage", ctx)
    out = buf.getvalue()
    assert "authoritative" in out.lower()
    assert "llm" in out and "0.0123" in out


@pytest.mark.asyncio
async def test_usage_falls_back_to_estimate():
    """No tracker / empty DB → estimate path, clearly labelled."""
    state = SessionState()
    state.tokens_in = 200
    state.tokens_out = 80
    state.cost_estimate_total = 0.05
    reg = default_registry()
    # /cost alias, no orchestrator at all → fallback
    ctx, buf = _plain_ctx(state=state, session_id="s1")
    await reg.dispatch("/cost", ctx)
    out = buf.getvalue()
    assert "estimate" in out.lower()
    assert "0.0500" in out


@pytest.mark.asyncio
async def test_usage_empty_breakdown_falls_back():
    class _Tracker:
        async def get_session_breakdown(self, sid):
            return {"by_type": []}

    orchestrator = type("O", (), {})()
    orchestrator.usage_tracker = _Tracker()
    state = SessionState()
    state.cost_estimate_total = 0.02
    reg = default_registry()
    ctx, buf = _plain_ctx(state=state, session_id="s1", orchestrator=orchestrator)
    await reg.dispatch("/usage", ctx)
    assert "estimate" in buf.getvalue().lower()


# ---------------------------------------------------------------------------
# /resume — replay against the real captured feed dir
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_replays_feed_dir(tmp_path, monkeypatch):
    # Build a small feed dir mirroring the real captured shape.
    feed = tmp_path / ".polyrob" / "sessions" / "local" / "mysession" / "feed"
    feed.mkdir(parents=True)
    (feed / "000001_session_start.json").write_text(
        json.dumps({"type": "session_start", "data": {"task": "t", "model_name": "m"}})
    )
    (feed / "000002_step_0001.json").write_text(
        json.dumps({
            "type": "step",
            "step": 1,
            "data": {"reasoning": "thinking hard", "actions": [
                {"action_type": "send_message", "name": "message", "service": "send",
                 "params": {"text": "hello"}}]},
        })
    )
    (feed / "000003_session_completion.json").write_text(
        json.dumps({"type": "session_completion", "data": {"success": True, "total_steps": 1,
                                                            "metrics": {"final_result": "done"}}})
    )

    # Force pm() resolution to miss so the CLI-store fallback (cwd/.polyrob) is used.
    monkeypatch.chdir(tmp_path)

    events_seen = []

    class _Recorder:
        verbose = False

        def on_event(self, event):
            events_seen.append(event)

        def print_block(self, text, **kw):
            events_seen.append(("block", text))

    reg = default_registry()
    ctx = CommandContext(renderer=_Recorder(), user_id="local")
    await reg.dispatch("/resume mysession", ctx)

    # session_start, step, session_completion → 3 normalized events replayed
    from cli.ui.events import SessionDone, SessionStart, Step

    types = [type(e) for e in events_seen if not isinstance(e, tuple)]
    assert SessionStart in types
    assert Step in types
    assert SessionDone in types


@pytest.mark.asyncio
async def test_resume_missing_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    reg = default_registry()
    ctx, buf = _plain_ctx(user_id="local")
    await reg.dispatch("/resume does-not-exist", ctx)
    out = buf.getvalue()
    # pm() may create an empty feed dir as a side effect, so "empty" is also a
    # valid not-found signal; either way nothing was replayed.
    assert ("No feed dir" in out) or ("is empty" in out)
    assert "replaying" not in out


@pytest.mark.asyncio
async def test_resume_no_arg():
    reg = default_registry()
    ctx, buf = _plain_ctx()
    await reg.dispatch("/resume", ctx)
    assert "Usage" in buf.getvalue()


# ---------------------------------------------------------------------------
# /cwd
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cwd(monkeypatch):
    class _PM:
        def get_workspace_dir(self, sid, uid):
            return Path("/tmp/ws") / sid

    monkeypatch.setattr("agents.task.path.pm", lambda: _PM())
    reg = default_registry()
    ctx, buf = _plain_ctx(session_id="abc")
    await reg.dispatch("/cwd", ctx)
    assert "/tmp/ws/abc" in buf.getvalue()


# ---------------------------------------------------------------------------
# /compress alias (Task 12)
# ---------------------------------------------------------------------------


def test_compress_registered_in_default_registry():
    """build_default_registry() must resolve /compress."""
    reg = build_default_registry()
    cmd = reg.lookup("compress")
    assert cmd is not None, "/compress not found in default registry"


def test_compress_has_same_handler_as_compact():
    """/compress and /compact must share the same handler."""
    reg = build_default_registry()
    compact_cmd = reg.lookup("compact")
    compress_cmd = reg.lookup("compress")
    assert compact_cmd is not None
    assert compress_cmd is not None
    assert compact_cmd.handler is compress_cmd.handler


def test_compact_still_works_after_compress_alias():
    """/compact must remain registered and unchanged."""
    reg = build_default_registry()
    cmd = reg.lookup("compact")
    assert cmd is not None
    assert cmd.name == "compact"
