"""A4: a profile-built (sub-agent) system prompt carries the session's real
context and the sub-agent communication contract.

The old ProfileManager call passed only action_description and
max_actions_per_step: a sub-agent got the JSON response format, every tool
advertised, no model-family note, the top-level "done is never delivered"
contract (its done text IS its report) and a <subtask-delegation> block a leaf
cannot use.
"""
from types import SimpleNamespace

from agents.task.agent.profile_manager import ProfileManager
from agents.task.agent.prompts import SystemPrompt


class _Controller:
    def list_tools(self):
        return ["filesystem", "web_fetch"]

    def get_prompt_action_index(self):
        return "ACTION INDEX"

    def supports_native_tools(self, provider):
        return True


def _profile(max_failures=3):
    return SimpleNamespace(llm={"model": "gpt-5", "provider": "openai"},
                           limits={"max_failures": max_failures})


def test_prompt_context_carries_the_real_session():
    llm = SimpleNamespace(model_name="gemini-2.5-pro")
    ctx = ProfileManager._prompt_context(
        {"injected_controller": _Controller(), "llm": llm, "is_sub_agent": True,
         "use_native_tools": True, "use_vision": False},
        _profile(), {"max_failures": 3})
    assert ctx["use_native_tools"] is True
    assert ctx["tool_ids"] == ["filesystem", "web_fetch"]
    assert ctx["model_name"] == "gemini-2.5-pro"
    assert ctx["sub_agent"] is True
    assert ctx["max_failures"] == 3
    assert ctx["action_description"] == "ACTION INDEX"


def test_prompt_context_falls_back_to_the_shared_controller():
    orch = SimpleNamespace(controller=_Controller())
    ctx = ProfileManager._prompt_context({"orchestrator": orch}, _profile(), {})
    assert ctx["tool_ids"] == ["filesystem", "web_fetch"]
    assert ctx["model_name"] == "gpt-5"  # the profile's own llm when none is given


def test_sub_agent_prompt_contract():
    ctx = ProfileManager._prompt_context(
        {"injected_controller": _Controller(), "is_sub_agent": True,
         "use_native_tools": True}, _profile(), {"max_failures": 3})
    ad = ctx.pop("action_description")
    text = SystemPrompt(ad, **ctx).get_system_message().content
    assert "IS your report to the parent" in text
    assert "send_message reaches no person" in text
    assert "the user never sees it" not in text
    assert "<subtask-delegation>" not in text
    assert "<message-shape>" not in text
    assert "3 consecutive failures end the session" in text
    assert "RESPONSE FORMAT" in text  # the native-tools format, not the JSON one
    # the family note's reply bullet does not reach a sub-agent
    assert "call send_message(text=" not in text


def test_top_level_prompt_is_unchanged_by_the_sub_agent_flag_default():
    text = SystemPrompt("x", use_native_tools=True, tool_ids=[]).get_system_message().content
    assert "IS your report to the parent" not in text
    assert "<message-shape>" in text
