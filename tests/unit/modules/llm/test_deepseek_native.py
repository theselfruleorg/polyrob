"""DeepSeek's own API as a first-class provider (2026-10-27).

Pins what DeepSeek differs in from the shared OpenAI-compatible pipeline:
the thinking-mode ``reasoning_content`` round-trip (the docs answer 400 when a
tool request omits it), no OpenRouter request extras, the cache-hit usage
field, the peak-hour price clock and the provider/model resolution.
"""
import logging
import types
from datetime import datetime, timezone

import pytest

from modules.llm.deepseek_client import DeepSeekClient, is_peak
from modules.llm.messages import AIMessage, HumanMessage, ToolMessage


def _client(model="deepseek-flash"):
    c = object.__new__(DeepSeekClient)
    c.logger = logging.getLogger("deepseek-native-test")
    c.model_type = model
    return c


def _response(reasoning="think first", tool_calls=None, usage=None):
    msg = types.SimpleNamespace(content="", reasoning_content=reasoning, tool_calls=tool_calls)
    return types.SimpleNamespace(id="r1", choices=[types.SimpleNamespace(message=msg)],
                                 usage=usage)


def _adapter(client):
    from modules.llm.adapters import OpenRouterAdapter
    return OpenRouterAdapter(client=client, model_name=client.model_type)


TC = [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]


def test_adapter_stamps_reasoning_on_its_own_message():
    c = _client()
    c.last_response = _response("plan: read the file")
    ai = AIMessage(content="")
    _adapter(c)._stamp_reasoning(ai)
    assert ai.additional_kwargs["reasoning_content"] == "plan: read the file"


def test_adapter_replays_reasoning_on_assistant_tool_turns():
    a = _adapter(_client())
    ai = AIMessage(content="x", tool_calls=TC)
    ai.additional_kwargs["reasoning_content"] = "plan"
    out = a._convert_to_client_messages(
        [HumanMessage(content="go"), ai, ToolMessage(content="ok", tool_call_id="c1")])
    assert out[1]["reasoning_content"] == "plan"
    assert "reasoning_content" not in out[0] and "reasoning_content" not in out[2]


def test_a_tool_turn_from_another_model_replays_an_empty_reasoning():
    """A resumed session or a /model switch: DeepSeek answers 400 when the field is
    missing on a tool-call turn and 200 when it is empty (checked on the live API)."""
    a = _adapter(_client())
    out = a._convert_to_client_messages(
        [HumanMessage(content="go"), AIMessage(content="", tool_calls=TC),
         ToolMessage(content="ok", tool_call_id="c1")])
    assert out[1]["reasoning_content"] == ""


def test_other_openai_compatible_clients_never_send_reasoning():
    from modules.llm.openrouter_client import OpenRouterClient
    c = object.__new__(OpenRouterClient)
    c.model_type = "z-ai/glm-5.2"
    c.last_response = _response("hidden")
    a = _adapter(c)
    ai = AIMessage(content="", tool_calls=TC)
    a._stamp_reasoning(ai)
    assert "reasoning_content" not in ai.additional_kwargs
    ai.additional_kwargs["reasoning_content"] = "hidden"
    out = a._convert_to_client_messages([ai])
    assert "reasoning_content" not in out[0]


def test_atomic_pair_keeps_reasoning_on_the_ai_message():
    from agents.task.agent.messages.builders import MessageBuildersMixin
    import threading

    class _MM(MessageBuildersMixin):
        def __init__(self):
            self.history = types.SimpleNamespace(messages=[], total_tokens=0)
            self._history_lock = threading.RLock()
            self.logger = logging.getLogger("mm")
            self.tool_call_tracker = None

        def _add_message_with_tokens(self, msg, **_):
            self.history.messages.append(msg)

    mm = _MM()
    mm.add_tool_call_pair_atomic("c", [{"id": "c1", "name": "read_file", "args": {}}],
                                 [("c1", "ok")], reasoning_content="plan")
    assert mm.history.messages[0].additional_kwargs["reasoning_content"] == "plan"


def test_request_extras_replace_openrouter_extras(monkeypatch):
    from modules.llm.openrouter_reasoning import apply_request_extras
    monkeypatch.setenv("OPENROUTER_REASONING_ENABLED", "false")
    body = apply_request_extras({"model": "deepseek-flash"}, _client(), 100)
    assert not (body.get("extra_body") or {}).keys() & {"usage", "reasoning", "provider"}


def test_usage_maps_cache_hits_and_bills_the_published_price(monkeypatch):
    import modules.llm.deepseek_client as dc
    c = _client()
    c.last_response = _response(usage=types.SimpleNamespace(
        prompt_tokens=1_000_000, completion_tokens=1_000_000, total_tokens=2_000_000,
        prompt_tokens_details=types.SimpleNamespace(cached_tokens=500_000),
        prompt_cache_hit_tokens=500_000, prompt_cache_miss_tokens=500_000))
    monkeypatch.setattr(dc, "is_peak", lambda: False)
    off = c._extract_usage_data()
    assert off["cached_tokens"] == 500_000
    # 0.5M miss x 0.15 + 0.5M hit x 0.003 + 1M out x 0.6
    assert off["billed_cost_usd"] == pytest.approx(0.075 + 0.0015 + 0.6)
    monkeypatch.setattr(dc, "is_peak", lambda: True)
    assert c._extract_usage_data()["billed_cost_usd"] == pytest.approx(2 * off["billed_cost_usd"])


@pytest.mark.parametrize("when,peak", [
    (datetime(2026, 10, 27, 1, 30, tzinfo=timezone.utc), True),    # Tue 01:30
    (datetime(2026, 10, 27, 9, 59, tzinfo=timezone.utc), True),    # Tue 09:59
    (datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc), False),    # Tue 04:00
    (datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc), False),   # Tue 10:00
    (datetime(2026, 10, 31, 7, 0, tzinfo=timezone.utc), False),    # Sat 07:00
])
def test_peak_windows(when, peak):
    assert is_peak(when) is peak


def test_factory_serves_deepseek_on_the_shared_adapter():
    from modules.llm.adapters import OpenRouterAdapter
    from modules.llm.llm_factory import create_chat_model
    c = _client()
    c.supports_vision = True
    m = create_chat_model("deepseek", "deepseek-flash", 0.7, llm_client=c)
    assert isinstance(m, OpenRouterAdapter)


def test_deepseek_key_alone_is_a_working_provider():
    from modules.llm.profiles import usable_providers_with_keys
    from modules.llm.llm_client_registry import get_default_model
    from modules.llm.model_registry import get_model_config, ModelProvider
    assert usable_providers_with_keys({"DEEPSEEK_API_KEY": "sk-" + "d" * 40}) == ["deepseek"]
    assert get_default_model("deepseek") == "deepseek-flash"
    for bare in ("deepseek-flash", "deepseek-chat", "deepseek-reasoner", "deepseek-v4-pro"):
        assert get_model_config(bare).provider == ModelProvider.DEEPSEEK


def test_native_tools_seams_stay_wired():
    # A provider missing from these two seams silently sends tools=0.
    from tools.controller.registry.schema_generators import get_schema_generator, OpenAISchemaGenerator
    from modules.llm.provider_spec import get_spec
    assert isinstance(get_schema_generator("deepseek"), OpenAISchemaGenerator)
    assert get_spec("deepseek").supports_native_tools is True


@pytest.mark.parametrize("typed,sent", [
    ("deepseek-v4.1-flash", "deepseek-flash"),   # catalog alias the API rejects (400)
    ("deepseek-4.1-flash", "deepseek-flash"),
    ("deepseek-pro", "deepseek-v4-pro"),
    ("deepseek-chat", "deepseek-chat"),          # legacy id: Flash, thinking OFF
    ("deepseek-reasoner", "deepseek-reasoner"),  # legacy id: Flash, thinking ON
    ("deepseek-v9", "deepseek-v9"),              # newer than the catalog: as typed
])
def test_client_sends_an_id_the_api_accepts(typed, sent):
    assert _client(typed).model_type == sent
