"""
Test suite for parse robustness in the task agent.

Modernized 2026-06 against the current API. Three tests were REMOVED because they
asserted behavior that has since been deleted/changed by design:
  - TestRetryLogic.test_get_next_action_retry_on_parse_error — mocked
    `message_manager.extract_json_from_model_output` as a METHOD; it is now a free
    function (`agents.task.utils_json.extract_json_from_model_output`), and the old
    `with_structured_output` retry shape no longer matches the native-tools path.
  - TestTokenManagement.test_cut_messages_called_before_llm — `MessageManager.cut_messages`
    no longer exists (token management was reorganized into the messages mixins).
  - TestExponentialBackoff.test_exponential_backoff_on_parse_error — asserted deterministic,
    no-jitter delays capped at 60s; the current LLM backoff is jittered and capped at 120s,
    and a plain ValueError is not on the LLM-backoff path at all.

This module now tests:
1. JSON extraction from various malformed outputs (the free function)
2. AgentOutput validation (now extra='forbid')
3. Tool-calling method selection per provider
"""
import pytest
from pydantic import ValidationError

from agents.task.agent.views import AgentOutput
from agents.task.utils_json import extract_json_from_model_output


class TestJSONExtraction:
    """JSON extraction robustness (utils_json.extract_json_from_model_output)."""

    def test_extract_json_from_json_code_block(self):
        content = '''Here's the response:
```json
{
  "current_state": {
    "page_summary": "Test page",
    "evaluation_previous_goal": "Success",
    "memory": "Test memory",
    "next_goal": "Next test",
    "reasoning": "Test reasoning"
  },
  "action": []
}
```
Some additional text'''
        result = extract_json_from_model_output(content)
        assert result['current_state']['page_summary'] == "Test page"
        assert result['action'] == []

    def test_extract_json_from_regular_code_block(self):
        # A regular (non-"json"-tagged) ``` fence around the full object.
        content = '''```
{
  "current_state": {
    "page_summary": "Test",
    "evaluation_previous_goal": "Success",
    "memory": "Memory",
    "next_goal": "Goal",
    "reasoning": "Reason"
  },
  "action": [{"test": {"param": "value"}}]
}
```'''
        result = extract_json_from_model_output(content)
        assert result['current_state']['page_summary'] == "Test"
        assert len(result['action']) == 1

    def test_extract_json_from_raw_content(self):
        content = '''{
  "current_state": {
    "page_summary": "Raw test",
    "evaluation_previous_goal": "Success",
    "memory": "Raw memory",
    "next_goal": "Raw goal",
    "reasoning": "Raw reason"
  },
  "action": []
}'''
        result = extract_json_from_model_output(content)
        assert result['current_state']['page_summary'] == "Raw test"

    def test_extract_json_with_extra_text(self):
        content = '''Let me analyze the current state:

{
  "current_state": {
    "page_summary": "Embedded test",
    "evaluation_previous_goal": "Success",
    "memory": "Embedded memory",
    "next_goal": "Embedded goal",
    "reasoning": "Embedded reason"
  },
  "action": []
}

That's my analysis.'''
        result = extract_json_from_model_output(content)
        assert result['current_state']['page_summary'] == "Embedded test"

    def test_extract_json_invalid_raises_error(self):
        with pytest.raises(ValueError, match="Could not parse response"):
            extract_json_from_model_output("This is not JSON at all")

    def test_extract_json_empty_content_raises_error(self):
        with pytest.raises(ValueError, match="Empty response from model"):
            extract_json_from_model_output("")


class TestAgentOutputValidation:
    """AgentOutput model validation (now ConfigDict(extra='forbid'))."""

    def test_agent_output_rejects_extra_fields(self):
        """AgentOutput now FORBIDS unknown fields (was: ignore). Extra => ValidationError."""
        data = {
            "current_state": {
                "page_summary": "Test",
                "evaluation_previous_goal": "Success",
                "memory": "Memory",
                "next_goal": "Goal",
                "reasoning": "Reason"
            },
            "action": [],
            "extra_field": "This should now be rejected",
            "another_extra": {"nested": "data"}
        }
        with pytest.raises(ValidationError):
            AgentOutput(**data)

    def test_agent_output_requires_core_fields(self):
        """AgentOutput still requires its core fields."""
        with pytest.raises(ValidationError):
            AgentOutput(**{"extra_field": "This is not enough"})


class TestToolCallingMethods:
    """Tool-calling method selection — all modern providers use native function_calling."""

    def _host(self, library):
        import logging
        from agents.task.agent.core.llm_provisioning import LLMProvisioningMixin

        host = LLMProvisioningMixin.__new__(LLMProvisioningMixin)
        host.chat_model_library = library
        host.logger = logging.getLogger("test_tool_calling")
        return host

    def test_tool_calling_enabled_for_all_providers(self):
        # Current native-tools adapters (Llama/LangChain providers were removed) plus an
        # unknown library, which must still default to function_calling.
        providers = [
            'ChatOpenAI',
            'ChatAnthropic',
            'ChatGoogleGenerativeAI',
            'ChatDeepSeek',
            'DeepSeekChatAdapter',
            'UnknownProvider',
        ]
        for provider in providers:
            host = self._host(provider)
            assert host.set_tool_calling_method('auto') == 'function_calling', (
                f"Expected function_calling for {provider}"
            )

    def test_explicit_method_passthrough(self):
        host = self._host('ChatOpenAI')
        assert host.set_tool_calling_method('json_mode') == 'json_mode'


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
