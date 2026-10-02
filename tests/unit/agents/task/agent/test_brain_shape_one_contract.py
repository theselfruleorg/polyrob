"""The native-tools brain-state shape is stated ONCE: the SystemPrompt's
RESPONSE FORMAT and the per-step reminder both name the nested
``{"current_state": {...}}`` object that ``extract_brain_state_from_json``
reads first (prompt-skill review 2026-09-29)."""
import json
import re

from agents.task.agent.core.next_action_internal import NATIVE_TOOLS_BRAIN_HINT
from agents.task.agent.prompts import SystemPrompt
from agents.task.utils_json import extract_brain_state_from_json


def _example(text: str) -> dict:
    m = re.search(r"\{\"current_state\".*\}\}", text)
    assert m, text
    return json.loads(m.group(0))


def test_reminder_names_the_nested_shape_the_parser_reads():
    ex = _example(NATIVE_TOOLS_BRAIN_HINT)
    assert set(ex) == {"current_state"}
    ex["current_state"]["memory"] = "did X"
    assert extract_brain_state_from_json(json.dumps(ex))["memory"] == "did X"


def test_system_prompt_and_reminder_agree():
    fmt = SystemPrompt("x", use_native_tools=True, tool_ids=[])._get_response_format_content()
    assert '"current_state"' in fmt
    for field in ("evaluation_previous_goal", "memory", "next_goal", "reasoning"):
        assert f'"{field}"' in fmt
        assert f'"{field}"' in NATIVE_TOOLS_BRAIN_HINT
