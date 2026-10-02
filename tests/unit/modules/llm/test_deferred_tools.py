"""F9 (063 WS-4, decided 2026-09-23) — the three late-tool modes and shape (b).

The owner's decision: the previous behaviour (``tools[]`` grows on ``load_tool``)
is the DEFAULT again, the schema-freeze bridge stays as an opt-in, and a capable
Anthropic model gets the native deferred-tools shape instead.

⚠️ NOT LIVE-TESTED. This tree has no Anthropic key, so no test here can prove
the PROVIDER honours ``defer_loading`` or that the cached prefix actually
survives a tool addition. What is asserted is the request we build, the request
we fall back to, and the self-heal — everything on our side of the wire.
"""
import pytest

from modules.llm.deferred_tools import (
    DEFERRED_TOOLS_BETA,
    LATE_TOOL_MODE_BRIDGE,
    LATE_TOOL_MODE_DEFERRED,
    LATE_TOOL_MODE_GROW,
    is_deferred_tools_error,
    late_tool_mode,
    model_supports_deferred_tools,
    prepare_deferred_tools,
    retry_params_without_deferral,
    deferred_tools_unavailable,
)


# --------------------------------------------------------------------------
# the ONE resolver
# --------------------------------------------------------------------------

def _clear(monkeypatch):
    monkeypatch.delenv("TOOL_SCHEMAS_FROZEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_DEFERRED_TOOLS", raising=False)


def test_default_is_grow_everywhere_but_anthropic(monkeypatch):
    _clear(monkeypatch)
    assert late_tool_mode("openai") == LATE_TOOL_MODE_GROW
    assert late_tool_mode("gemini") == LATE_TOOL_MODE_GROW
    assert late_tool_mode("") == LATE_TOOL_MODE_GROW


def test_default_is_deferred_on_anthropic(monkeypatch):
    _clear(monkeypatch)
    assert late_tool_mode("anthropic") == LATE_TOOL_MODE_DEFERRED
    assert late_tool_mode("Anthropic") == LATE_TOOL_MODE_DEFERRED


def test_frozen_flag_wins_on_every_provider(monkeypatch):
    """An operator who asks for the provider-neutral bridge gets it everywhere."""
    _clear(monkeypatch)
    monkeypatch.setenv("TOOL_SCHEMAS_FROZEN", "true")
    assert late_tool_mode("anthropic") == LATE_TOOL_MODE_BRIDGE
    assert late_tool_mode("openai") == LATE_TOOL_MODE_BRIDGE


def test_deferred_can_be_turned_off_back_to_grow(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_DEFERRED_TOOLS", "false")
    assert late_tool_mode("anthropic") == LATE_TOOL_MODE_GROW


def test_model_capability_reads_the_registry():
    assert model_supports_deferred_tools("claude-opus-5") is True
    assert model_supports_deferred_tools("claude-fable-5-1") is True
    assert model_supports_deferred_tools("claude-opus-4-8") is False
    assert model_supports_deferred_tools("claude-sonnet-4-6") is False
    assert model_supports_deferred_tools("gpt-4.1") is False
    assert model_supports_deferred_tools(None) is False


# --------------------------------------------------------------------------
# the wire transform
# --------------------------------------------------------------------------

ENVELOPE = "<tool-addition>\nlate_echo\nlate_ping\n</tool-addition>"


def _tools(deferred=("late_echo",)):
    out = [{"name": "done", "description": "d", "input_schema": {"type": "object"}}]
    for name in deferred:
        out.append({"name": name, "description": "d",
                    "input_schema": {"type": "object"}, "defer_loading": True})
    return out


def _params(messages, tools=None):
    return {"model": "claude-opus-5", "messages": messages,
            "tools": tools if tools is not None else _tools()}


def test_envelope_becomes_a_system_tool_addition_block():
    params = _params([
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": "ok"},
        # a tool result — role `user` on the Anthropic wire
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1"}]},
        {"role": "user", "content": ENVELOPE},
        {"role": "assistant", "content": "next"},
    ])
    out = prepare_deferred_tools(params, enabled=True)
    system = [m for m in out["messages"] if m["role"] == "system"]
    assert len(system) == 1
    assert system[0]["content"] == [
        {"type": "tool_addition", "tool": {"type": "tool_reference", "name": "late_echo"}},
        {"type": "tool_addition", "tool": {"type": "tool_reference", "name": "late_ping"}},
    ]
    # It stayed in place: it follows a user turn and is followed by an assistant.
    assert out["messages"][3]["role"] == "system"


def test_an_unplaceable_addition_falls_back_instead_of_400ing():
    """No legal slot (the tail is an assistant turn) -> send no deferral at all."""
    params = _params([
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": ENVELOPE},
        {"role": "assistant", "content": "next"},
    ])
    out = prepare_deferred_tools(params, enabled=True)
    assert all(m["role"] != "system" for m in out["messages"])
    assert all("defer_loading" not in t for t in out["tools"])
    assert "extra_headers" not in out


def test_a_system_message_followed_by_a_user_message_moves_to_the_end():
    """Anthropic's placement rule: last entry, or followed by an assistant."""
    params = _params([
        {"role": "user", "content": "do the thing"},
        {"role": "user", "content": ENVELOPE},
        {"role": "user", "content": "<state>step 3</state>"},
    ])
    out = prepare_deferred_tools(params, enabled=True)
    roles = [m["role"] for m in out["messages"]]
    assert roles == ["user", "user", "system"]
    assert out["messages"][-1]["content"][0]["type"] == "tool_addition"


def test_an_envelope_at_index_zero_is_dropped():
    """A system message may never be messages[0]."""
    params = _params([
        {"role": "user", "content": ENVELOPE},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "<state>step 2</state>"},
    ])
    out = prepare_deferred_tools(params, enabled=True)
    assert all(m["role"] != "system" for m in out["messages"])
    assert len(out["messages"]) == 2


def test_the_beta_header_is_requested():
    params = _params([
        {"role": "user", "content": "hi"},
        {"role": "user", "content": ENVELOPE},
        {"role": "user", "content": "<state/>"},
    ])
    out = prepare_deferred_tools(params, enabled=True)
    assert out["extra_headers"]["anthropic-beta"] == DEFERRED_TOOLS_BETA


def test_the_header_is_requested_for_a_deferred_tool_with_no_addition_yet():
    params = _params([{"role": "user", "content": "hi"}])
    out = prepare_deferred_tools(params, enabled=True)
    assert out["extra_headers"]["anthropic-beta"] == DEFERRED_TOOLS_BETA


def test_an_existing_beta_header_is_preserved():
    params = _params([{"role": "user", "content": "hi"}])
    params["extra_headers"] = {"anthropic-beta": "other-beta-2026-01-01"}
    out = prepare_deferred_tools(params, enabled=True)
    assert out["extra_headers"]["anthropic-beta"] == (
        f"other-beta-2026-01-01,{DEFERRED_TOOLS_BETA}")


def test_the_memoized_tools_list_is_never_mutated():
    """`tools` is the registry's shared per-provider memo (L2 no-mutate rule)."""
    tools = _tools()
    snapshot = [dict(t) for t in tools]
    params = _params([{"role": "user", "content": "hi"}], tools=tools)
    prepare_deferred_tools(params, enabled=False)
    prepare_deferred_tools(params, enabled=True)
    assert tools == snapshot
    assert params["messages"] == [{"role": "user", "content": "hi"}]


def test_at_least_one_tool_must_stay_non_deferred():
    """Every tool deferred is not a legal request — fall back whole."""
    tools = [{"name": "a", "input_schema": {}, "defer_loading": True},
             {"name": "b", "input_schema": {}, "defer_loading": True}]
    params = _params([
        {"role": "user", "content": "hi"},
        {"role": "user", "content": ENVELOPE},
        {"role": "user", "content": "<state/>"},
    ], tools=tools)
    out = prepare_deferred_tools(params, enabled=True)
    assert all("defer_loading" not in t for t in out["tools"])
    assert all(m["role"] != "system" for m in out["messages"])
    assert "extra_headers" not in out


def test_a_cache_breakpoint_on_the_envelope_moves_to_the_last_non_system_message():
    """F12: a breakpoint may never ride the system message we build."""
    marked = {"role": "user", "content": [
        {"type": "text", "text": ENVELOPE,
         "cache_control": {"type": "ephemeral", "ttl": "1h"}}]}
    params = _params([
        {"role": "user", "content": "hi"},
        marked,
        {"role": "user", "content": "<state/>"},
    ])
    out = prepare_deferred_tools(params, enabled=True)
    assert out["messages"][-1]["role"] == "system"
    tail = out["messages"][-2]
    assert tail["content"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


# --------------------------------------------------------------------------
# the fallback shape
# --------------------------------------------------------------------------

def test_disabled_strips_every_trace_and_matches_a_plain_request():
    plain_messages = [
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "<state/>"},
    ]
    plain = {"model": "claude-opus-5", "messages": list(plain_messages),
             "tools": [{"name": "done", "description": "d",
                        "input_schema": {"type": "object"}},
                       {"name": "late_echo", "description": "d",
                        "input_schema": {"type": "object"}}]}

    deferred = _params(plain_messages[:2] + [
        {"role": "user", "content": ENVELOPE}] + plain_messages[2:])
    deferred["tools"] = [
        {"name": "done", "description": "d", "input_schema": {"type": "object"}},
        {"name": "late_echo", "description": "d",
         "input_schema": {"type": "object"}, "defer_loading": True}]

    out = prepare_deferred_tools(deferred, enabled=False)
    assert out["tools"] == plain["tools"]
    assert out["messages"] == plain["messages"]
    assert "extra_headers" not in out


def test_disabled_also_undoes_an_already_transformed_request():
    """The self-heal rebuilds a request this module ALREADY transformed."""
    params = _params([
        {"role": "user", "content": "hi"},
        {"role": "user", "content": ENVELOPE},
        {"role": "user", "content": "<state/>"},
    ])
    once = prepare_deferred_tools(params, enabled=True)
    twice = prepare_deferred_tools(once, enabled=False)
    assert all(m["role"] != "system" for m in twice["messages"])
    assert all("defer_loading" not in t for t in twice["tools"])
    assert "extra_headers" not in twice


def test_a_message_that_merely_mentions_the_tag_is_left_alone():
    body = "look at this: <tool-addition>\nnot a name list here\n</tool-addition>"
    params = _params([
        {"role": "user", "content": "hi"},
        {"role": "user", "content": body},
        {"role": "user", "content": "<state/>"},
    ])
    out = prepare_deferred_tools(params, enabled=True)
    assert out["messages"][1]["content"] == body


# --------------------------------------------------------------------------
# the 400 self-heal
# --------------------------------------------------------------------------

class _BadRequestError(Exception):
    status_code = 400


def _deferred_params():
    return _params([
        {"role": "user", "content": "hi"},
        {"role": "user", "content": ENVELOPE},
        {"role": "user", "content": "<state/>"},
    ])


class _Client:
    _SUPPORTS_DEFERRED_TOOLS = True
    model_type = "claude-opus-5"

    def __init__(self, failures):
        self._failures = failures
        self.requests = []
        self._client = self

    @property
    def messages(self):
        return self

    async def create(self, **params):
        self.requests.append(params)
        if self._failures:
            self._failures -= 1
            raise _BadRequestError(
                "tools.1.defer_loading: Extra inputs are not permitted")
        return {"ok": True}


def test_error_recognition_requires_a_400_and_the_vocabulary():
    assert is_deferred_tools_error(
        _BadRequestError("unexpected field defer_loading")) is True
    assert is_deferred_tools_error(
        _BadRequestError("something else entirely")) is False
    other = Exception("defer_loading")
    assert is_deferred_tools_error(other) is False  # no 400, no BadRequest class


@pytest.mark.asyncio
async def test_the_self_heal_retries_once_without_deferral():
    from modules.llm.deferred_tools import call_with_deferral_retry
    client = _Client(failures=1)
    params = prepare_deferred_tools(_deferred_params(), enabled=True)
    result = await call_with_deferral_retry(client, params, use_streaming=False)
    assert result == {"ok": True}
    assert len(client.requests) == 2
    second = client.requests[1]
    assert all("defer_loading" not in t for t in second["tools"])
    assert all(m["role"] != "system" for m in second["messages"])
    assert "extra_headers" not in second
    assert deferred_tools_unavailable(client) is True


@pytest.mark.asyncio
async def test_an_unrelated_400_propagates_untouched():
    from modules.llm.deferred_tools import call_with_deferral_retry

    class _Boom(_Client):
        async def create(self, **params):
            self.requests.append(params)
            raise _BadRequestError("max_tokens must be greater than thinking budget")

    client = _Boom(failures=0)
    params = prepare_deferred_tools(_deferred_params(), enabled=True)
    with pytest.raises(_BadRequestError):
        await call_with_deferral_retry(client, params, use_streaming=False)
    assert len(client.requests) == 1
    assert deferred_tools_unavailable(client) is False


def test_no_retry_when_the_request_carried_no_deferral():
    client = _Client(failures=0)
    plain = {"model": "m", "messages": [{"role": "user", "content": "hi"}],
             "tools": [{"name": "done", "input_schema": {}}]}
    assert retry_params_without_deferral(
        client, _BadRequestError("defer_loading"), plain) is None
    assert deferred_tools_unavailable(client) is False
