"""The action narrator (043 A14) — one human line per tool action, copy-driven.

The three anchor lines come straight from
``docs/design/040/web/chat-running.html``; the rest guard the hard rules the
mockup's note states: one line, no ``[step]``/``[event]``/``[iter]`` trace, no
``repr()`` of the event, no raw machine name, and a failure that reads as one.
"""
import re

from agents.task.telemetry.narrate import narrate
from core.copy import STRINGS, t


# --------------------------------------------------------- the three anchor lines


def test_files_read_matches_the_mockup_line():
    ev = {
        "type": "tool_execution",
        "data": {"tool_name": "filesystem", "action_name": "read_file", "success": True},
        "render": {"kind": "file", "payload": {"count": 4, "where": "~/price-watch"}},
    }
    assert narrate(ev) == "Read 4 files in ~/price-watch"


def test_tests_all_passed_matches_the_mockup_line():
    ev = {
        "type": "tool_execution",
        "data": {"tool_name": "coding", "action_name": "run_tests", "success": True},
        "render": {"kind": "log", "payload": {"passed": 18, "failed": 0}},
    }
    assert narrate(ev) == "Ran the test suite and all 18 passed"


def test_running_deploy_matches_the_mockup_line():
    # A started action has no `success` yet -> the running phrasing.
    ev = {"type": "tool_started", "data": {"tool_name": "app_service", "action_name": "deploy"}}
    assert narrate(ev) == "Building the container"


# ------------------------------------------------------------- the fallback rules


def test_unknown_tool_is_a_safe_generic_line():
    ev = {"data": {"tool_name": "some_new_tool", "action_name": "frobnicate", "success": True}}
    line = narrate(ev)
    assert line == "Ran a tool"
    # never the machine names, never an unfilled placeholder, never a trace token
    assert "some_new_tool" not in line
    assert "frobnicate" not in line
    assert "{" not in line and "}" not in line


def test_unknown_running_tool_is_a_safe_generic_line():
    ev = {"data": {"tool_name": "some_new_tool", "action_name": "frobnicate"}}
    assert narrate(ev) == "Working on the task"


def test_failure_reads_as_a_failure_and_never_leaks_the_error():
    ev = {
        "data": {
            "tool_name": "filesystem",
            "action_name": "read_file",
            "success": False,
            "error": "Traceback (most recent call last): KeyError('x')",
        }
    }
    line = narrate(ev)
    assert line == "An action did not finish"
    assert "Traceback" not in line and "KeyError" not in line


def test_never_emits_a_trace_token_or_a_repr_on_any_shape():
    weird = [None, 123, "not a dict", {}, {"data": {}}, {"data": {"tool_name": "x"}},
             {"render": {"kind": "text"}}, {"tool_name": "filesystem", "action_name": "read_file"}]
    for ev in weird:
        line = narrate(ev)
        assert isinstance(line, str) and line.strip()
        assert "\n" not in line
        for token in ("[step]", "[event]", "[iter]", "{'", "': "):
            assert token not in line
        # no bracketed trace tag of any kind
        assert not re.search(r"\[(step|event|iter)\b", line)


# ------------------------------------------------------------- richer coverage


def test_single_read_derives_the_directory_from_the_path_without_render():
    # pre-A16 shape: no render block, just the parameters the tool ran with.
    ev = {
        "data": {
            "tool_name": "filesystem",
            "action_name": "read_file",
            "success": True,
            "parameters": {"path": "~/price-watch/app.py"},
        }
    }
    assert narrate(ev) == "Read one file in ~/price-watch"


def test_flat_data_dict_is_accepted_directly():
    flat = {"tool_name": "coding", "action_name": "run_tests", "success": True,
            "passed": 3, "failed": 2}
    assert narrate(flat) == "Ran the test suite, 3 passed and 2 failed"


def test_write_file_names_the_basename_only():
    ev = {"data": {"tool_name": "filesystem", "action_name": "write_file", "success": True,
                   "parameters": {"path": "~/price-watch/report.md"}}}
    assert narrate(ev) == "Wrote report.md"


def test_run_code_running_and_done():
    running = {"data": {"tool_name": "code_execution", "action_name": "run_code"}}
    done = {"data": {"tool_name": "code_execution", "action_name": "run_code", "success": True}}
    assert narrate(running) == "Running the code"
    assert narrate(done) == "Ran the code"


def test_files_read_without_a_place_omits_the_in_clause():
    ev = {"render": {"payload": {"count": 4}},
          "data": {"tool_name": "filesystem", "action_name": "read_file", "success": True}}
    assert narrate(ev) == "Read 4 files"


def test_a_long_or_multiline_place_is_dropped_not_rendered():
    ev = {"data": {"tool_name": "filesystem", "action_name": "read_file", "success": True,
                   "parameters": {"path": "~/x/" + "a" * 300 + "/f.py"}}}
    line = narrate(ev)
    assert line == "Read one file"  # the oversize directory is not put on the line


# ------------------------------------------------------------- the words are copy


def test_every_line_is_a_core_copy_string_not_a_hardcode():
    # The narrator must not bake phrasing; every anchor resolves to a chat.act key.
    assert t("chat.act.deploy_running") == "Building the container"
    assert t("chat.act.tests_all_passed", passed=18) == "Ran the test suite and all 18 passed"
    assert t("chat.act.did_work") == "Ran a tool"
    for key in ("chat.act.files_read", "chat.act.tests_all_passed",
                "chat.act.deploy_running", "chat.act.working", "chat.act.did_work",
                "chat.act.failed"):
        assert key in STRINGS
