"""What a model thought stays with the seat that wrote it (106 L5).

Anthropic's signed blocks have their own key, and reasoning text names its seat (only native
DeepSeek writes and reads it), so a failover or a /model change sends a provider nothing
that another one wrote.
"""
import json
import logging
from types import SimpleNamespace

import pytest

from modules.llm.adapters import AnthropicAdapter, OpenRouterAdapter
from modules.llm.anthropic_client import AnthropicClient
from modules.llm.call_result import replay_fields
from modules.llm.adapters import DeepSeekAdapter
from modules.llm.deepseek_client import DeepSeekClient
from modules.llm.messages import AIMessage, HumanMessage, ToolMessage
from modules.llm.openrouter_client import OpenRouterClient

CALLS = [{"id": "u1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]
SIGNED = SimpleNamespace(type="thinking", thinking="a secret plan", signature="sig-1")
USE = SimpleNamespace(type="tool_use", id="u1", name="t", input={})


def _client(cls, model):
    client = cls.__new__(cls)
    client.logger = logging.getLogger("test.replay")
    client.model_type = model
    return client


def _adapter(cls, adapter_cls, model):
    return adapter_cls(client=_client(cls, model), model_name=model)


def _from_claude():
    message = AIMessage(content="", tool_calls=list(CALLS))
    _adapter(AnthropicClient, AnthropicAdapter, "claude-opus-5-5")._stamp_reasoning(
        message, SimpleNamespace(content=[SIGNED, USE]))
    return message


@pytest.mark.parametrize("cls,adapter_cls,model", [
    (DeepSeekClient, DeepSeekAdapter, "deepseek-flash"),
    (OpenRouterClient, OpenRouterAdapter, "deepseek/deepseek-v4.1-flash"),
])
def test_a_failover_from_anthropic_sends_none_of_its_blocks(cls, adapter_cls, model):
    wire = _adapter(cls, adapter_cls, model)._convert_to_client_messages(
        [HumanMessage(content="go"), _from_claude(), ToolMessage(content="r", tool_call_id="u1")])
    assert "a secret plan" not in json.dumps(wire) and "sig-1" not in json.dumps(wire)
    assert not wire[1].get("reasoning_content")
    assert "thinking_blocks" not in wire[1]


def test_anthropic_keeps_its_blocks_under_their_own_key():
    message = _from_claude()
    assert "reasoning_content" not in message.additional_kwargs
    assert message.additional_kwargs["anthropic_thinking"][0]["block"]["signature"] == "sig-1"
    wire = _adapter(AnthropicClient, AnthropicAdapter, "claude-opus-5-5")._convert_to_client_messages([message])
    assert wire[0]["thinking_blocks"][0]["before_tool"] == "u1"


def test_anthropic_reads_no_reasoning_text():
    message = AIMessage(content="a")
    message.additional_kwargs.update(reasoning_content="why", reasoning_seat="deepseek-native")
    wire = _adapter(AnthropicClient, AnthropicAdapter, "claude-opus-5-5")._convert_to_client_messages([message])
    assert "thinking_blocks" not in wire[0] and "reasoning_content" not in wire[0]


def test_deepseek_replays_its_own_text_on_another_model():
    message = AIMessage(content="a")
    message.additional_kwargs.update(reasoning_content="why", reasoning_seat="deepseek-native")
    wire = _adapter(DeepSeekClient, DeepSeekAdapter, "deepseek-pro")._convert_to_client_messages([message])
    assert wire[0]["reasoning_content"] == "why"


def test_deepseek_gets_an_empty_field_for_text_of_another_seat():
    message = AIMessage(content="a")
    message.additional_kwargs.update(reasoning_content="why", reasoning_family="glm")
    wire = _adapter(DeepSeekClient, DeepSeekAdapter, "deepseek-flash")._convert_to_client_messages([message])
    assert wire[0]["reasoning_content"] == ""


def test_replay_fields_hold_only_the_replay_keys_with_a_value():
    assert replay_fields({"reasoning_content": "x", "reasoning_seat": "deepseek-native", "anthropic_thinking": [],
                          "reasoning_family": "glm", "usage": {"a": 1}}) == {
        "reasoning_content": "x", "reasoning_seat": "deepseek-native"}
    assert replay_fields(None) == {}
