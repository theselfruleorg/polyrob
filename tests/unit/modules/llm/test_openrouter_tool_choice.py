"""B-3 (review 2026-10-14, owner approved): `tool_choice` on agent steps.

13.4 % of agent calls were text-only "empty actions" while the OpenRouter client
always sent `tool_choice:"auto"`. An agent step (the toolset holds `done`) now
asks for `required`; every other caller keeps `auto`; an upstream that rejects
`required` is retried once with `auto` and remembered per model.
"""
import asyncio
import types

import pytest

from modules.llm import openrouter_routing as rt

DONE = {"type": "function", "function": {"name": "done", "parameters": {}}}
READ = {"type": "function", "function": {"name": "read_file", "parameters": {}}}


@pytest.fixture(autouse=True)
def _clean():
    rt._REQUIRED_REJECTED.clear()
    yield
    rt._REQUIRED_REJECTED.clear()


def test_resolve_rules():
    assert rt.resolve_tool_choice([READ, DONE], "m") == "required"
    assert rt.resolve_tool_choice([READ], "m") == "auto"
    assert rt.resolve_tool_choice([READ, DONE], "m", explicit="auto") == "auto"
    assert rt.resolve_tool_choice([READ], "m", explicit="required") == "required"
    assert rt.resolve_tool_choice([{"name": "done"}], "m") == "required"   # flat shape
    rt.remember_required_rejected("m")
    assert rt.resolve_tool_choice([READ, DONE], "m") == "auto"
    assert rt.resolve_tool_choice([READ, DONE], "other") == "required"


class _Completions:
    def __init__(self, errors=()):
        self.calls = []
        self._errors = list(errors)

    async def create(self, **params):
        self.calls.append(dict(params))
        if self._errors:
            raise self._errors.pop(0)
        msg = types.SimpleNamespace(content="", tool_calls=None)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)],
                                     usage=None, id="gen-1")


def _client(completions, label="OpenRouter"):
    from modules.llm.openrouter_client import OpenRouterClient
    c = OpenRouterClient.__new__(OpenRouterClient)
    c.model_type = "deepseek/deepseek-v4.1-flash"
    c.max_tokens = 128
    c.temperature = 0.2
    c.last_response = None
    c._initialized = True
    c._PROVIDER_LABEL = label
    c.logger = types.SimpleNamespace(debug=lambda *a, **k: None, info=lambda *a, **k: None,
                                     warning=lambda *a, **k: None, error=lambda *a, **k: None)
    c._extract_usage_and_capture_telemetry = lambda *a, **k: None
    c._client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=completions))
    return c


def _run(c, tools, **kw):
    return asyncio.run(c._generate_with_tools([{"role": "user", "content": "go"}], tools=tools, **kw))


def test_agent_step_sends_required_and_other_toolsets_send_auto():
    comp = _Completions()
    c = _client(comp)
    _run(c, [READ, DONE])
    _run(c, [READ])
    assert [call["tool_choice"] for call in comp.calls] == ["required", "auto"]


def test_explicit_parameter_wins():
    comp = _Completions()
    _run(_client(comp), [READ, DONE], tool_choice="auto")
    assert comp.calls[0]["tool_choice"] == "auto"


def test_subclass_seat_keeps_auto_unless_asked():
    comp = _Completions()
    c = _client(comp, label="NVIDIA")
    _run(c, [READ, DONE])
    _run(c, [READ, DONE], tool_choice="required")
    assert [call["tool_choice"] for call in comp.calls] == ["auto", "required"]


def test_rejection_retries_once_with_auto_and_is_remembered():
    err = RuntimeError("Error code: 400 - tool_choice 'required' is not supported by this provider")
    comp = _Completions(errors=[err])
    c = _client(comp)
    _run(c, [READ, DONE])
    assert [call["tool_choice"] for call in comp.calls] == ["required", "auto"]
    _run(c, [READ, DONE])
    assert comp.calls[-1]["tool_choice"] == "auto", "the model is remembered for the process"
    assert len(comp.calls) == 3


def test_other_errors_are_not_swallowed():
    from core.exceptions import LLMError
    comp = _Completions(errors=[RuntimeError("Error code: 502 upstream down")])
    with pytest.raises(LLMError):
        _run(_client(comp), [READ, DONE])
    assert len(comp.calls) == 1 and not rt._REQUIRED_REJECTED
