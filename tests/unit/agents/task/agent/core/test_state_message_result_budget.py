"""057 WS-B follow-up (prod 2026-09-20 14:04Z): the state message re-rendered
tool results RAW, so `TOOL_RESULT_MAX_TOKENS` capped only the ToolMessage.

`result_processing._pair_results_to_calls` cuts the ToolMessage string to the
cap; then `AgentMessagePrompt` prints the SAME `extracted_content` again as
`Action result N/M:` under a separate 100k-CHAR limit (`MAX_SUCCESS_LENGTH`).
Five `anysite_api` JSON answers in one step (cron `acf52a31`, session
`ab28c032`) put ~110k uncached tokens into step 7 → 210,220 input tokens and a
221 s LLM timeout, with the 6k-token cap "armed". The render site now applies
the SAME cap when it is on; cap off ⇒ the legacy char limit, byte-identical.
"""
from unittest.mock import patch

from agents.task.agent.prompts import AgentMessagePrompt
from tools.controller.types import ActionResult


class _Tree:
    def clickable_elements_to_string(self, include_attributes=None):
        return ""


class _State:
    def __init__(self):
        self.url = ""
        self.title = "No Browser"
        self.tabs = []
        self.screenshot = None
        self.pixels_above = 0
        self.pixels_below = 0
        self.element_tree = _Tree()


def _render(results):
    return AgentMessagePrompt(state=_State(), result=results).get_user_message(use_vision=False).content


def test_state_message_honours_the_tool_result_token_cap(monkeypatch):
    monkeypatch.setenv("TOOL_RESULT_MAX_TOKENS", "100")        # 400 chars
    big = "x" * 20_000
    text = _render([ActionResult(extracted_content=big)])
    body = text.split("Action result 1/1: ", 1)[1]
    assert body.count("x") <= 400
    assert "truncated: 100 of 5,000 tokens" in body            # the SAME honest tail as the ToolMessage


def test_state_message_cap_applies_per_result(monkeypatch):
    monkeypatch.setenv("TOOL_RESULT_MAX_TOKENS", "100")
    text = _render([ActionResult(extracted_content="y" * 20_000) for _ in range(5)])
    assert text.count("y") <= 5 * 400
    assert text.count("[…truncated:") == 5


def test_cap_off_keeps_the_legacy_char_limit(monkeypatch):
    monkeypatch.setenv("TOOL_RESULT_MAX_TOKENS", "0")
    from agents.task.robust_parse_config import RobustParseConfig
    big = "z" * (RobustParseConfig.MAX_SUCCESS_LENGTH + 500)
    text = _render([ActionResult(extracted_content=big)])
    assert text.count("z") == RobustParseConfig.MAX_SUCCESS_LENGTH
    assert "[...truncated 500 chars]" in text
    assert "[…truncated:" not in text
