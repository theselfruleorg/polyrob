"""Only native DeepSeek sends stored reasoning back. Prod 2026-11-07: an OpenRouter replay
raised reasoning about threefold and fed a loop of one repeated word up to the output cap."""
import json
import logging
from types import SimpleNamespace

from modules.llm.adapters import OpenRouterAdapter
from modules.llm.adapters import DeepSeekAdapter
from modules.llm.deepseek_client import DeepSeekClient
from modules.llm.llm_factory import _openrouter_adapter
from modules.llm.messages import AIMessage, HumanMessage
from modules.llm.nvidia_client import NvidiaClient
from modules.llm.openrouter_client import OpenRouterClient


def _client(cls=OpenRouterClient, model="deepseek/deepseek-v4.1-flash"):
    client = cls.__new__(cls)
    client.logger = logging.getLogger("test.replay")
    client.model_type = model
    return client


def _raw(**message):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(**message))])


def _history(seat="deepseek-native"):
    thought = AIMessage(content="a", tool_calls=[{"id": "1", "type": "function", "function": {"name": "t", "arguments": "{}"}}])
    thought.additional_kwargs.update(reasoning_content="line one\n  line two é", reasoning_seat=seat)
    return [HumanMessage(content="hi"), thought, AIMessage(content="plain")]


def test_the_factory_gives_the_replay_adapter_only_to_native_deepseek(monkeypatch):
    monkeypatch.setattr("modules.llm.llm_factory._sync_client_model", lambda *a, **k: None)
    assert type(_openrouter_adapter("deepseek-flash", _client(DeepSeekClient, "deepseek-flash"), {},
                                    sync_capabilities=False)) is DeepSeekAdapter
    for cls in (OpenRouterClient, NvidiaClient):
        assert type(_openrouter_adapter("m", _client(cls), {}, sync_capabilities=False)) is OpenRouterAdapter


def test_openrouter_stores_and_sends_no_reasoning_even_from_an_old_session():
    message = AIMessage(content="")
    OpenRouterAdapter(_client(), model_name="m")._stamp_reasoning(message, _raw(reasoning_content="why"))
    assert "reasoning_content" not in message.additional_kwargs
    for cls in (OpenRouterClient, NvidiaClient):
        wire = OpenRouterAdapter(_client(cls), model_name="m")._convert_to_client_messages(_history())
        assert all("reasoning_content" not in m for m in wire)


def test_deepseek_stamps_its_seat():
    message = AIMessage(content="")
    DeepSeekAdapter(_client(DeepSeekClient, "deepseek-flash"), model_name="m")._stamp_reasoning(
        message, _raw(reasoning_content="why"))
    assert message.additional_kwargs == {"reasoning_content": "why", "reasoning_seat": "deepseek-native"}


def test_deepseek_replay_holds_its_bytes_and_sends_the_empty_field():
    adapter = DeepSeekAdapter(_client(DeepSeekClient, "deepseek-flash"), model_name="m")
    wire = adapter._convert_to_client_messages(_history())
    assert wire[1]["reasoning_content"] == "line one\n  line two é"
    assert wire[2]["reasoning_content"] == ""
    assert json.dumps(wire) == json.dumps(adapter._convert_to_client_messages(_history()))


def test_deepseek_never_gets_text_that_openrouter_wrote_into_an_old_history():
    """The 11-07 loop text sits in old histories tagged only by model family."""
    old = AIMessage(content="a")
    old.additional_kwargs.update(reasoning_content="Hmm. Hmm. Hmm.", reasoning_family="deepseek")
    adapter = DeepSeekAdapter(_client(DeepSeekClient, "deepseek-flash"), model_name="m")
    assert adapter._convert_to_client_messages([old])[0]["reasoning_content"] == ""
