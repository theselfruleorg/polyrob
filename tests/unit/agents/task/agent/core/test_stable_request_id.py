"""G-26 reachability fix (Task 5c, commit c3d7ed49 follow-up).

The dedup built in Task 5c (a real `request_id` column + partial unique index
+ INSERT OR IGNORE in `usage_tracker._write_to_database`) was UNREACHABLE:
`record_llm_usage` had no `request_id` parameter and `_generate_request_id()`
returned a fresh `uuid.uuid4().hex` on EVERY call, so two billings of the SAME
completion always got different ids and the unique index never collided.

This suite covers the fix: `extract_stable_request_id` (the STABLE
idempotency-key extractor, agents/task/agent/core/aux_metering.py) and its
wiring into `meter_aux_llm`.
"""
import types

import pytest

from agents.task.agent.core.aux_metering import extract_stable_request_id, meter_aux_llm


# ── extract_stable_request_id: pure extraction logic ────────────────────────

def test_prefers_id_on_response_object_itself():
    response = types.SimpleNamespace(id="msg_direct_on_response")
    llm = types.SimpleNamespace(_client=types.SimpleNamespace(last_response=None))
    assert extract_stable_request_id(llm, response, "anthropic") == \
        "resp:anthropic:msg_direct_on_response"


def test_falls_back_to_client_last_response_object_id():
    """The realistic path: AIMessage never carries `.id` (adapters never set
    it), but the underlying client's raw SDK response does -- e.g. Anthropic's
    Message object (`msg_...`) or OpenAI's ChatCompletion (`chatcmpl_...`)."""
    response = types.SimpleNamespace(content="hi")  # no .id, like a real AIMessage
    raw = types.SimpleNamespace(id="msg_01AbC")
    llm = types.SimpleNamespace(_client=types.SimpleNamespace(last_response=raw))
    assert extract_stable_request_id(llm, response, "anthropic") == "resp:anthropic:msg_01AbC"


def test_falls_back_to_client_last_response_dict_id():
    """DeepSeek's raw response is a parsed JSON dict, not an SDK object."""
    response = types.SimpleNamespace(content="hi")
    llm = types.SimpleNamespace(_client=types.SimpleNamespace(
        last_response={"id": "chatcmpl-deepseek-123", "choices": []}
    ))
    assert extract_stable_request_id(llm, response, "deepseek") == \
        "resp:deepseek:chatcmpl-deepseek-123"


def test_structured_output_dict_shape_checks_raw_key():
    """{'raw': <sdk response>, 'parsed': ...} -- the (mostly dead) structured-
    output fallback shape. `raw` should be checked before falling through to
    the client's last_response."""
    response = {"raw": types.SimpleNamespace(id="msg_from_raw_key"), "parsed": object()}
    llm = types.SimpleNamespace(_client=types.SimpleNamespace(
        last_response=types.SimpleNamespace(id="msg_should_not_be_used")
    ))
    assert extract_stable_request_id(llm, response, "anthropic") == \
        "resp:anthropic:msg_from_raw_key"


def test_no_id_anywhere_returns_none_not_a_fabricated_key():
    """HONESTY requirement: never synthesize a fake key. None means the caller
    falls back to record_llm_usage's fresh-uuid legacy behavior."""
    response = types.SimpleNamespace(content="hi")
    llm = types.SimpleNamespace(_client=types.SimpleNamespace(last_response=None))
    assert extract_stable_request_id(llm, response, "gemini") is None


def test_no_llm_object_returns_none():
    response = types.SimpleNamespace(content="hi")
    assert extract_stable_request_id(None, response, "anthropic") is None


def test_empty_string_id_is_not_treated_as_stable():
    response = types.SimpleNamespace(id="")
    llm = types.SimpleNamespace(_client=types.SimpleNamespace(last_response=None))
    assert extract_stable_request_id(llm, response, "openai") is None


def test_namespaced_by_provider_so_cross_provider_ids_cannot_collide():
    raw_id = "same-literal-id"
    llm = types.SimpleNamespace(_client=types.SimpleNamespace(
        last_response=types.SimpleNamespace(id=raw_id)
    ))
    response = types.SimpleNamespace(content="hi")
    key_a = extract_stable_request_id(llm, response, "openai")
    key_b = extract_stable_request_id(llm, response, "anthropic")
    assert key_a != key_b
    assert key_a == f"resp:openai:{raw_id}"
    assert key_b == f"resp:anthropic:{raw_id}"


# ── Fix pass 2: per-call stamped attribute takes priority over the shared,
#    concurrency-racy `<client>.last_response` read (money-correctness) ────

def test_stamped_response_attribute_is_preferred_over_response_id_and_last_response():
    """When adapters.py has stamped `_polyrob_provider_response_id` onto the
    per-call response object, that MUST win over both a generic `.id` on the
    same object and the shared client's `last_response` -- it is the most
    reliable, per-call source."""
    response = types.SimpleNamespace(id="msg_generic_id")
    response._polyrob_provider_response_id = "msg_stamped"
    llm = types.SimpleNamespace(_client=types.SimpleNamespace(
        last_response=types.SimpleNamespace(id="msg_last_response")
    ))
    assert extract_stable_request_id(llm, response, "openai") == "resp:openai:msg_stamped"


def test_absent_stamped_attribute_falls_back_to_old_logic():
    """No `_polyrob_provider_response_id` on the response (e.g. a hand-built
    test double, or a provider path that predates the stamp) -> falls through
    to the pre-existing response.id / last_response fallback chain,
    unchanged."""
    response = types.SimpleNamespace(content="hi")  # no stamped attr, no .id
    raw = types.SimpleNamespace(id="msg_from_last_response")
    llm = types.SimpleNamespace(_client=types.SimpleNamespace(last_response=raw))
    assert extract_stable_request_id(llm, response, "anthropic") == \
        "resp:anthropic:msg_from_last_response"


def test_concurrent_shared_client_prefers_stamped_response_over_last_response():
    """Regression proof for the under-billing bug fixed in pass 2.

    Story: two sub-agents (A and B) run concurrently and SHARE one LLM
    client object (SubAgentManager.run_subtask inherits `parent_agent.llm`
    verbatim when a subtask has no own model -- SUB_AGENTS_ENABLED default
    True, MAX_CONCURRENT_SUB_AGENTS=3). A's completion finishes, but by the
    time A's billing code runs, B's completion has already landed on the
    shared client and overwritten `_client.last_response` with B's raw
    response. Before pass 2, extracting A's request_id from
    `_client.last_response` would silently return B's id -- a DIFFERENT
    completion's id -- causing the dedup's INSERT OR IGNORE to (wrongly)
    treat A's billing row as a duplicate of a row that was never inserted
    under A's key, or worse, collide two distinct completions onto one key.

    Simulate the race directly: `last_response` points at B's raw response,
    but the caller is billing A's response object, which (correctly, per the
    adapters.py fix) carries A's own stamped id. Extraction MUST return A's
    id, never B's.
    """
    response_a = types.SimpleNamespace(content="A's completion")
    response_a._polyrob_provider_response_id = "msg_A"
    raw_b = types.SimpleNamespace(id="msg_B")
    llm = types.SimpleNamespace(_client=types.SimpleNamespace(last_response=raw_b))

    result = extract_stable_request_id(llm, response_a, "anthropic")

    assert result == "resp:anthropic:msg_A"
    assert result != "resp:anthropic:msg_B"


def test_two_distinct_stamped_responses_never_collide_even_sharing_one_client():
    """Two genuinely distinct completions on the SAME shared client, each
    with its own stamped id, must extract to two distinct keys -- proving
    the dedup can't falsely collapse two real, billable completions into
    one ledger row (the actual under-billing mechanism this fix closes)."""
    shared_client = types.SimpleNamespace(last_response=types.SimpleNamespace(id="msg_whatever_is_currently_there"))
    llm = types.SimpleNamespace(_client=shared_client)

    response_a = types.SimpleNamespace(content="a")
    response_a._polyrob_provider_response_id = "msg_A"
    response_b = types.SimpleNamespace(content="b")
    response_b._polyrob_provider_response_id = "msg_B"

    key_a = extract_stable_request_id(llm, response_a, "openai")
    key_b = extract_stable_request_id(llm, response_b, "openai")

    assert key_a == "resp:openai:msg_A"
    assert key_b == "resp:openai:msg_B"
    assert key_a != key_b


def test_resp_prefix_can_never_collide_with_uuid_hex_fallback():
    """`_generate_request_id()` returns a bare uuid.hex (32 lowercase hex
    chars, no colons) -- structurally disjoint from `resp:...`."""
    import uuid
    fallback = uuid.uuid4().hex
    llm = types.SimpleNamespace(_client=types.SimpleNamespace(
        last_response=types.SimpleNamespace(id="msg_x")
    ))
    real = extract_stable_request_id(llm, types.SimpleNamespace(content="hi"), "anthropic")
    assert real != fallback
    assert real.startswith("resp:")
    assert not fallback.startswith("resp:")


# ── meter_aux_llm: wiring into record_llm_usage's request_id kwarg ──────────

@pytest.mark.asyncio
async def test_meter_aux_llm_passes_extracted_provider_id_as_request_id():
    """A billing call site: mock a response carrying a provider id and spy
    record_llm_usage's request_id argument."""
    captured = {}

    class _Tracker:
        async def record_llm_usage(self, **kwargs):
            captured.update(kwargs)

    raw = types.SimpleNamespace(id="msg_abc")
    llm = types.SimpleNamespace(
        model_name="claude-x", llm_provider="anthropic",
        _client=types.SimpleNamespace(last_response=raw),
    )
    response = types.SimpleNamespace(
        content="hi", usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    )

    await meter_aux_llm(
        usage_tracker=_Tracker(), user_id="u1", session_id="s1", agent_id="a1",
        llm=llm, response=response, duration_seconds=1.0,
        component="judge", purpose="output_validation",
    )

    assert captured.get("request_id") == "resp:anthropic:msg_abc"
    assert captured.get("model") == "claude-x"
    assert captured.get("provider") == "anthropic"


@pytest.mark.asyncio
async def test_meter_aux_llm_passes_none_request_id_when_no_stable_id_available():
    """No provider id anywhere -> request_id=None, so record_llm_usage falls
    back to its fresh-uuid legacy path (no false dedup across distinct aux
    calls that happen to share tokens/model/etc)."""
    captured = {}

    class _Tracker:
        async def record_llm_usage(self, **kwargs):
            captured.update(kwargs)

    llm = types.SimpleNamespace(
        model_name="gemini-x", llm_provider="gemini",
        _client=types.SimpleNamespace(last_response=None),
    )
    response = types.SimpleNamespace(
        content="hi", usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    )

    await meter_aux_llm(
        usage_tracker=_Tracker(), user_id="u1", session_id="s1", agent_id="a1",
        llm=llm, response=response, duration_seconds=1.0,
        component="compaction", purpose="compaction",
    )

    assert captured.get("request_id") is None
