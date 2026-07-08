"""T1-06 / T1-11 (2026-07-06 structural review): the prompt must not advertise
capabilities the session lacks.

T1-06: browser/vision/<input-format> sections were injected unconditionally — the
include_browser_tools/include_vision gating params had no non-default caller
(~620 wasted tokens/session + vision claimed for use_vision=False sessions).
T1-11: the no-MCP fallback ("use perplexity_search / Browser actions") rendered
even when neither tool was loaded, contradicting <using-your-tools>.

Gate = the anysite pattern: the session's real tool_ids. tool_ids=None (legacy
callers that never pass it) keeps every section — byte-identical back-compat.
"""
from unittest.mock import MagicMock

from agents.task.agent.prompts import SystemPrompt


def _render(**kw) -> str:
    sp = SystemPrompt(action_description="- done(text): finish the task",
                      use_native_tools=True, **kw)
    return sp.get_system_message().content


# ---------------------------------------------------------------- T1-06 browser

def test_browser_sections_absent_without_browser_tool():
    c = _render(tool_ids=["filesystem", "task", "web_fetch"])
    assert "<browser-tools>" not in c
    assert "browser_go_to_url" not in c
    # <input-format> describes browser state (URL/tabs/interactive elements) —
    # pure noise for a session that cannot drive a browser.
    assert "<input-format>" not in c
    assert "Interactive Elements" not in c


def test_browser_sections_present_with_browser_tool():
    c = _render(tool_ids=["browser", "filesystem", "task"])
    assert "<browser-tools>" in c
    assert "<input-format>" in c


def test_no_tool_ids_keeps_browser_sections_backcompat():
    c = _render()  # legacy caller: tool_ids not passed
    assert "<browser-tools>" in c
    assert "<input-format>" in c


def test_explicit_include_browser_tools_false_still_wins():
    c = _render(include_browser_tools=False, tool_ids=["browser"])
    assert "<browser-tools>" not in c


# ---------------------------------------------------------- T1-06 web routing

def test_web_access_routing_survives_without_browser():
    # web_fetch/perplexity are loaded: the tier-routing guidance must still teach
    # them (it used to live only inside <browser-tools>), and be honest that
    # browser interaction is NOT available this session.
    c = _render(tool_ids=["filesystem", "web_fetch", "perplexity"])
    assert "fetch_url" in c
    assert "<web-access>" in c
    assert "not loaded" in c.lower()  # honest about the missing interact tier


def test_web_access_absent_when_no_web_tools():
    c = _render(tool_ids=["filesystem", "task"])
    assert "<web-access>" not in c
    assert "fetch_url" not in c


# ---------------------------------------------------------------- T1-06 vision

def test_vision_section_gated_on_include_vision():
    assert "YOU HAVE VISION" not in _render(include_vision=False)
    assert "YOU HAVE VISION" in _render(include_vision=True)


def test_message_manager_forwards_include_vision_to_system_prompt():
    # The real fix: MessageManager (the only production caller) must thread the
    # session's use_vision through — the param existed but had no caller.
    from agents.task.agent.message_manager.service import MessageManager

    captured = {}

    class FakePrompt:
        def __init__(self, action_descriptions, **kw):
            captured.update(kw)

        def get_system_message(self):
            from modules.llm.messages import SystemMessage
            return SystemMessage(content="sys")

    llm = MagicMock()
    llm.model_type = "gpt-4"
    MessageManager(
        llm=llm,
        task="t",
        action_descriptions="x",
        system_prompt_class=FakePrompt,
        include_vision=False,
    )
    assert captured.get("include_vision") is False


# ------------------------------------------------------------ T1-11 MCP fallback

def test_no_mcp_fallback_only_names_loaded_tools():
    c = _render(tool_ids=["perplexity", "filesystem", "task"])
    assert "perplexity_search" in c
    assert "Browser actions" not in c  # browser not loaded — don't advertise it


def test_no_mcp_fallback_dropped_when_no_alternatives_loaded():
    c = _render(tool_ids=["filesystem", "task"])
    assert "perplexity_search" not in c
    assert "MCP tools are not currently configured" not in c


def test_no_mcp_fallback_backcompat_without_tool_ids():
    c = _render()
    assert "MCP tools are not currently configured" in c
