"""Tests for the --toolset CLI option (run command) and /toolset slash command.

Covers:
- polyrob run --help shows --toolset option.
- polyrob run --toolset research parses and resolves the toolset.
- /toolset (no arg) lists all named toolsets.
- /toolset <name> validates + persists a "session.toolset" preference (owner-UX
  P2 T6 — the arg branch is a pref-setting SWITCH, not a read-only detail
  view; see tests/unit/cli/test_persona_toolset_switch.py for the full
  valid/invalid + honest-message contract).
- /toolset <unknown> is rejected, listing the valid toolset names.
"""
import pytest
import unittest.mock as mock
from click.testing import CliRunner


# ---------------------------------------------------------------------------
# run --toolset (CLI option parse)
# ---------------------------------------------------------------------------

def test_run_help_shows_toolset_option():
    """polyrob run --help includes --toolset."""
    from cli.polyrob import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "--help"])
    assert result.exit_code == 0
    assert "--toolset" in result.output


def test_run_toolset_option_parses(monkeypatch):
    """polyrob run --toolset research parses and forwards toolset to _run_session."""
    from cli.polyrob import cli

    captured = {}

    async def fake_run_session(task, model, provider, tools, toolset, max_steps, plain,
                               verbose, resume_id=None):
        captured["tools"] = tools
        captured["toolset"] = toolset

    monkeypatch.setattr("cli.commands.run._run_session", fake_run_session)

    runner = CliRunner()
    result = runner.invoke(cli, ["run", "--toolset", "research", "do something"])
    assert result.exit_code == 0
    assert captured.get("toolset") == "research"
    assert captured.get("tools") is None


def test_resolve_tool_list_toolset_resolves():
    """_resolve_tool_list(None, 'research') resolves to the research set (pruned)."""
    from cli.commands.run import _resolve_tool_list
    from agents.task.tool_defaults import resolve_toolset
    from core.bootstrap import cli_unavailable_tools

    tool_list, _notes = _resolve_tool_list(None, "research")

    expected = resolve_toolset("research")
    unavail = set(cli_unavailable_tools(expected))
    expected_pruned = [t for t in expected if t not in unavail]
    assert tool_list == expected_pruned


def test_resolve_tool_list_tools_takes_precedence_over_toolset():
    """When both --tools and --toolset are given, the RESOLVED list is the --tools list.

    Asserts the actual resolved tool_list (not just arg passing): with
    --tools filesystem,task + --toolset research the result is ['filesystem','task'],
    NOT the research set.
    """
    from cli.commands.run import _resolve_tool_list

    tool_list, _notes = _resolve_tool_list("filesystem,task", "research")
    assert tool_list == ["filesystem", "task"]
    # The research-only members must be absent (tools won).
    assert "perplexity" not in tool_list
    assert "anysite" not in tool_list
    assert "browser" not in tool_list


# ---------------------------------------------------------------------------
# /toolset slash command handler
# ---------------------------------------------------------------------------

def _make_ctx(args=None, *, home_dir=None):
    """Build a minimal CommandContext for handler tests.

    ``home_dir`` (when given) wires ``ctx.container.config.data_dir`` so a
    write-behavior test (the ``args`` branch persists a preference) never
    touches the real ``data/`` tree — pass ``tmp_path`` for any test that
    invokes the arg branch with a KNOWN toolset name (persists).
    """
    from types import SimpleNamespace
    from cli.ui.commands.registry import CommandContext
    container = None
    if home_dir is not None:
        container = SimpleNamespace(config=SimpleNamespace(data_dir=home_dir))
    ctx = CommandContext(args=args or [], container=container)
    return ctx


def test_toolset_handler_no_args_lists_toolsets(capsys):
    """_h_toolset() with no args emits the list of named toolsets."""
    from cli.ui.commands.handlers import _h_toolset

    output_lines = []

    def fake_emit(text, *, title="", style=""):
        output_lines.append(text)

    ctx = _make_ctx(args=[])
    ctx.emit = fake_emit  # type: ignore

    # Patch cli_unavailable_tools to return nothing (all available).
    with mock.patch("core.bootstrap.cli_unavailable_tools", return_value=[]):
        _h_toolset(ctx)

    combined = "\n".join(output_lines)
    # Should mention several toolset names (incl. social).
    assert "research" in combined
    assert "coding" in combined
    assert "minimal" in combined
    assert "social" in combined
    # Must surface the "new sessions only / polyrob run" guidance line.
    assert "polyrob run --toolset" in combined.lower()


def test_toolset_handler_with_name_persists_and_shows_resolved_ids(tmp_path, capsys):
    """_h_toolset(['research']) persists session.toolset and confirms with the
    resolved ids + an honest next-session message (owner-UX P2 T6)."""
    from core.prefs import load_preferences
    from cli.ui.commands.handlers import _h_toolset

    output_lines = []

    def fake_emit(text, *, title="", style=""):
        output_lines.append(text)

    ctx = _make_ctx(args=["research"], home_dir=tmp_path)
    ctx.emit = fake_emit  # type: ignore

    with mock.patch("core.bootstrap.cli_unavailable_tools", return_value=[]):
        _h_toolset(ctx)

    combined = "\n".join(output_lines)
    assert "filesystem" in combined
    assert "task" in combined
    assert "perplexity" in combined
    assert "anysite" in combined
    assert "web_fetch" in combined  # research now uses the lightweight web reader, not browser
    # Must show guidance (next session — no live switching).
    assert "next session" in combined.lower() or "polyrob run" in combined.lower()
    assert load_preferences(tmp_path, "local")["session.toolset"] == "research"


def test_toolset_handler_unknown_name_rejected_lists_valid_names(tmp_path):
    """_h_toolset(['no_such_set']) is REJECTED (no fallback, no persist) and
    lists the valid toolset names."""
    from core.prefs import load_preferences
    from agents.task.tool_defaults import TOOLSETS
    from cli.ui.commands.handlers import _h_toolset

    output_lines = []

    def fake_emit(text, *, title="", style=""):
        output_lines.append(text)

    ctx = _make_ctx(args=["no_such_set"], home_dir=tmp_path)
    ctx.emit = fake_emit  # type: ignore

    with mock.patch("core.bootstrap.cli_unavailable_tools", return_value=[]):
        _h_toolset(ctx)

    combined = "\n".join(output_lines)
    assert "unknown" in combined.lower()
    for name in TOOLSETS:
        assert name in combined
    assert "session.toolset" not in load_preferences(tmp_path, "local")


def test_toolset_handler_registered_in_default_registry():
    """The /toolset command must be registered in the default REPL command registry."""
    from cli.ui.commands.handlers import build_default_registry, reset_default_registry

    reset_default_registry()
    reg = build_default_registry()
    names = {cmd.name for cmd in reg.commands()}
    assert "toolset" in names


def test_tools_show_accepts_run_surface_browser_alias():
    """`tools show browser` (the id `run --tools browser` uses) resolves to the
    catalog's `browser_manager` instead of erroring 'unknown tool'."""
    from click.testing import CliRunner
    from cli.commands.tools import tools
    res = CliRunner().invoke(tools, ["show", "browser"])
    assert res.exit_code == 0, res.output
    assert "unknown tool" not in res.output.lower()
    assert "browser_manager" in res.output
