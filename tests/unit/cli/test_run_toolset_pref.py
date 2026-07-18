"""owner-UX P1 T5: session.toolset pref wiring in cli.commands.run._resolve_tool_list().

No user_id, or user_id with no pref file, => byte-identical to the pre-existing
(--tools/--toolset/default) precedence covered by test_toolset_command.py. A
written pref overrides the DEFAULT toolset NAME only when neither --tools nor
--toolset was passed for this invocation; an explicit --tools/--toolset always
still wins over the pref.
"""
from core.prefs import write_preference
from core.bootstrap import cli_unavailable_tools
from agents.task.tool_defaults import cli_default_tools, resolve_toolset
from cli.commands.run import _resolve_tool_list


def test_no_user_id_is_legacy_unchanged():
    tool_list, _notes = _resolve_tool_list(None, None)
    assert tool_list == cli_default_tools()


def test_no_pref_file_is_legacy_unchanged(tmp_path):
    tool_list, _notes = _resolve_tool_list(None, None, user_id="u1", home_dir=tmp_path)
    assert tool_list == cli_default_tools()


def test_pref_overrides_default_toolset(tmp_path):
    write_preference(tmp_path, "u1", "session.toolset", "coding")
    tool_list, _notes = _resolve_tool_list(None, None, user_id="u1", home_dir=tmp_path)
    expected = resolve_toolset("coding")
    unavail = set(cli_unavailable_tools(expected))
    assert tool_list == [t for t in expected if t not in unavail]
    assert tool_list != cli_default_tools()


def test_explicit_toolset_still_wins_over_pref(tmp_path):
    write_preference(tmp_path, "u1", "session.toolset", "coding")
    tool_list, _notes = _resolve_tool_list(None, "research", user_id="u1", home_dir=tmp_path)
    expected = resolve_toolset("research")
    unavail = set(cli_unavailable_tools(expected))
    assert tool_list == [t for t in expected if t not in unavail]


def test_explicit_tools_still_wins_over_pref(tmp_path):
    write_preference(tmp_path, "u1", "session.toolset", "coding")
    tool_list, _notes = _resolve_tool_list(
        "filesystem,task", None, user_id="u1", home_dir=tmp_path
    )
    assert tool_list == ["filesystem", "task"]
