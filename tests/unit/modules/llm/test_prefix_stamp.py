"""prefix_sha / tools_sha — the request's prefix identity, stamped for billing."""
import types

from modules.llm.call_result import CallFacts, cold_call_cause, read_stamps
from modules.llm.call_scope import begin_call, set_foundation_len
from modules.llm.prefix_stamp import compute_stamps, stamp_request


def _req(system="You are Nova.", tools=None):
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": "go"}]
    r = {"model": "m", "messages": msgs}
    if tools is not None:
        r["tools"] = tools
    return r


def test_same_bytes_same_stamp_different_bytes_different_stamp():
    a = compute_stamps(_req(tools=[{"type": "function", "function": {"name": "x"}}]))
    b = compute_stamps(_req(tools=[{"type": "function", "function": {"name": "x"}}]))
    c = compute_stamps(_req(system="You are Nova. Today is 2026-09-20.", tools=[{"type": "function", "function": {"name": "x"}}]))
    d = compute_stamps(_req(tools=[{"type": "function", "function": {"name": "y"}}]))
    assert a == b and len(a["prefix_sha"]) == 12 and len(a["tools_sha"]) == 12
    assert a["prefix_sha"] != c["prefix_sha"] and a["tools_sha"] == c["tools_sha"]
    assert a["tools_sha"] != d["tools_sha"] and a["prefix_sha"] == d["prefix_sha"]


def test_missing_parts_are_absent_not_digests_of_nothing():
    assert compute_stamps({"messages": [{"role": "user", "content": "hi"}]}) == {}
    assert "tools_sha" not in compute_stamps(_req())
    assert compute_stamps({}) == {}


def test_stamps_ride_the_call_and_read_back_from_its_message():
    request = begin_call()
    stamps = stamp_request(_req(tools=[{"a": 1}]))
    assert request.stamps == stamps and set(stamps) == {"prefix_sha", "tools_sha"}
    message = types.SimpleNamespace(call_facts=CallFacts(request_stamps=stamps))
    assert read_stamps(message) == stamps
    assert read_stamps(types.SimpleNamespace()) == {}          # a message with no facts is silent
    stamp_request({"messages": []})                            # a retry had no system msg
    assert request.stamps == {}


def _foundation_req(first="identity", tail="go"):
    return {"messages": [{"role": "system", "content": "s"}, {"role": "user", "content": first},
                         {"role": "user", "content": tail}]}


def test_foundation_sha_covers_the_rows_after_the_system_message():
    set_foundation_len(1)
    try:
        base = compute_stamps(_foundation_req())["foundation_sha"]
        assert compute_stamps(_foundation_req(tail="other"))["foundation_sha"] == base
        assert compute_stamps(_foundation_req(first="changed"))["foundation_sha"] != base
    finally:
        set_foundation_len(None)
    assert "foundation_sha" not in compute_stamps(_foundation_req())


def test_a_moving_cache_marker_does_not_change_the_foundation_sha():
    set_foundation_len(1)
    try:
        plain = compute_stamps(_foundation_req())["foundation_sha"]
        marked = _foundation_req()
        marked["messages"][1]["content"] = [{"type": "text", "text": "identity",
                                             "cache_control": {"type": "ephemeral"}}]
        assert compute_stamps(marked)["foundation_sha"] == plain
    finally:
        set_foundation_len(None)


_HIT = {"input_tokens": 1000, "cached_tokens": 900, "model": "m", "upstream": "A",
        "tools_sha": "t", "foundation_sha": "f", "prefix_sha": "p"}


def _cold(**over):
    return {**_HIT, "cached_tokens": 10, **over}


def test_no_cause_with_no_previous_call_or_a_warm_call():
    assert cold_call_cause(None, _cold()) is None
    assert cold_call_cause(_HIT, dict(_HIT)) is None
    assert cold_call_cause({**_HIT, "cached_tokens": 100}, _cold()) is None, "the previous call was cold"


def test_each_cold_call_names_its_cause():
    assert cold_call_cause(_HIT, _cold(upstream="B")) == "upstream_moved"
    assert cold_call_cause(_HIT, _cold(model="other")) == "upstream_moved"
    assert cold_call_cause(_HIT, _cold(tools_sha="x")) == "tools_changed"
    assert cold_call_cause(_HIT, _cold(foundation_sha="x")) == "foundation_changed"
    assert cold_call_cause(_HIT, _cold(prefix_sha="x")) == "foundation_changed"
    assert cold_call_cause(_HIT, _cold(input_tokens=300)) == "compaction"
    assert cold_call_cause(_HIT, _cold()) == "expired_or_unknown"


# ---------------------------------------------------------------------------
# F20 (2026-09-22): the stamp was OpenAI-shaped, so it silently came back EMPTY
# on the Anthropic and Responses paths — exactly where the money-rail first-call
# cache misses were measured. compute_stamps now reads whatever shape the
# provider uses.
# ---------------------------------------------------------------------------


def test_anthropic_shaped_params_yield_a_prefix_sha():
    """Anthropic puts the system prompt in a top-level `system` key."""
    out = compute_stamps({"model": "claude", "system": "You are Nova.",
                          "messages": [{"role": "user", "content": "go"}],
                          "tools": [{"name": "x"}]})
    assert len(out["prefix_sha"]) == 12 and len(out["tools_sha"]) == 12
    # the SAME text through the OpenAI shape hashes identically — one prefix
    # identity across providers, not one per transport.
    assert out["prefix_sha"] == compute_stamps(_req(system="You are Nova."))["prefix_sha"]


def test_anthropic_block_list_system_is_hashed_stably():
    blocks = [{"type": "text", "text": "part one"},
              {"type": "text", "text": "part two"}]
    a = compute_stamps({"system": blocks})
    b = compute_stamps({"system": [dict(x) for x in blocks]})
    assert a["prefix_sha"] == b["prefix_sha"]
    assert a["prefix_sha"] != compute_stamps({"system": blocks[:1]})["prefix_sha"]


def test_responses_api_instructions_are_the_prefix():
    out = compute_stamps({"model": "gpt", "instructions": "You are Nova.",
                          "input": [{"role": "user", "content": "go"}]})
    assert out["prefix_sha"] == compute_stamps(_req(system="You are Nova."))["prefix_sha"]


def test_explicit_system_argument_wins():
    out = compute_stamps({"messages": [{"role": "system", "content": "ignored"}]},
                         system="You are Nova.")
    assert out["prefix_sha"] == compute_stamps(_req(system="You are Nova."))["prefix_sha"]


def test_a_request_with_no_prefix_anywhere_is_still_silent():
    assert compute_stamps({"input": [{"role": "user", "content": "go"}]}) == {}
    assert compute_stamps({"system": ""}) == {}
