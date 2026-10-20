"""Coherency review 2026-10-13 E-A5: ageing a wrapped result kept only its first
line — the open ``<untrusted_tool_result>`` tag — so the model kept zero data and
an unclosed frame sat in the middle of history."""

from core.security.untrusted_wrap import wrap_untrusted
from agents.task.agent.messages.filters import AGED_RESULT_MARKER, _aged_body, age_old_tool_results
from modules.llm.messages import AIMessage, ToolMessage


def test_aged_untrusted_result_keeps_data_and_recloses_the_frame():
    content = wrap_untrusted("web_fetch", "ETH price 2,412 USD at 10:00\n" + "row\n" * 2000)
    aged = _aged_body(content)
    lines = aged.split("\n")
    assert lines[0] == '<untrusted_tool_result source="web_fetch">'
    assert lines[1] == "ETH price 2,412 USD at 10:00"
    assert lines[-1] == "</untrusted_tool_result>"
    assert aged.count("<untrusted_tool_result") == aged.count("</untrusted_tool_result>") == 1
    assert AGED_RESULT_MARKER in aged


def test_json_result_keeps_its_first_data_line():
    aged = _aged_body('{\n  "balance": "1.25 ETH",\n' + '  "x": 1,\n' * 1000 + "}")
    assert aged.split("\n")[0] == '"balance": "1.25 ETH",'


def test_plain_result_unchanged_shape():
    aged = _aged_body("HEAD LINE\n" + "y" * 5000)
    assert aged.startswith("HEAD LINE\n" + AGED_RESULT_MARKER)


def test_ageing_a_framed_result_is_idempotent():
    msgs = [AIMessage(content="", tool_calls=[{"id": "c1", "name": "web_fetch", "args": {}}]),
            ToolMessage(content=wrap_untrusted("web_fetch", "data\n" + "z" * 9000), tool_call_id="c1")]
    once = age_old_tool_results(msgs, keep_recent=0)
    twice = age_old_tool_results(once, keep_recent=0)
    assert once[1].content == twice[1].content
    assert len(once[1].content) < 2000
