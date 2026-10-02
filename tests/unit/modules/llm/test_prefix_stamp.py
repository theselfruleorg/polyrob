"""prefix_sha / tools_sha — the request's prefix identity, stamped for billing."""
import types

from modules.llm.prefix_stamp import compute_stamps, read_stamps, stamp_client


def _req(system="You are Rob.", tools=None):
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": "go"}]
    r = {"model": "m", "messages": msgs}
    if tools is not None:
        r["tools"] = tools
    return r


def test_same_bytes_same_stamp_different_bytes_different_stamp():
    a = compute_stamps(_req(tools=[{"type": "function", "function": {"name": "x"}}]))
    b = compute_stamps(_req(tools=[{"type": "function", "function": {"name": "x"}}]))
    c = compute_stamps(_req(system="You are Rob. Today is 2026-09-20.", tools=[{"type": "function", "function": {"name": "x"}}]))
    d = compute_stamps(_req(tools=[{"type": "function", "function": {"name": "y"}}]))
    assert a == b and len(a["prefix_sha"]) == 12 and len(a["tools_sha"]) == 12
    assert a["prefix_sha"] != c["prefix_sha"] and a["tools_sha"] == c["tools_sha"]
    assert a["tools_sha"] != d["tools_sha"] and a["prefix_sha"] == d["prefix_sha"]


def test_missing_parts_are_absent_not_digests_of_nothing():
    assert compute_stamps({"messages": [{"role": "user", "content": "hi"}]}) == {}
    assert "tools_sha" not in compute_stamps(_req())
    assert compute_stamps({}) == {}


def test_stamps_ride_the_client_and_read_back_through_the_adapter():
    client = types.SimpleNamespace()
    stamps = stamp_client(client, _req(tools=[{"a": 1}]))
    llm = types.SimpleNamespace(_client=client)
    assert read_stamps(llm) == stamps and set(stamps) == {"prefix_sha", "tools_sha"}
    assert read_stamps(types.SimpleNamespace()) == {}          # an unstamped path is silent
    stamp_client(client, {"messages": []})                     # the next call had no system msg
    assert read_stamps(llm) == {}


# ---------------------------------------------------------------------------
# F20 (2026-09-22): the stamp was OpenAI-shaped, so it silently came back EMPTY
# on the Anthropic and Responses paths — exactly where the money-rail first-call
# cache misses were measured. compute_stamps now reads whatever shape the
# provider uses.
# ---------------------------------------------------------------------------


def test_anthropic_shaped_params_yield_a_prefix_sha():
    """Anthropic puts the system prompt in a top-level `system` key."""
    out = compute_stamps({"model": "claude", "system": "You are Rob.",
                          "messages": [{"role": "user", "content": "go"}],
                          "tools": [{"name": "x"}]})
    assert len(out["prefix_sha"]) == 12 and len(out["tools_sha"]) == 12
    # the SAME text through the OpenAI shape hashes identically — one prefix
    # identity across providers, not one per transport.
    assert out["prefix_sha"] == compute_stamps(_req(system="You are Rob."))["prefix_sha"]


def test_anthropic_block_list_system_is_hashed_stably():
    blocks = [{"type": "text", "text": "part one"},
              {"type": "text", "text": "part two"}]
    a = compute_stamps({"system": blocks})
    b = compute_stamps({"system": [dict(x) for x in blocks]})
    assert a["prefix_sha"] == b["prefix_sha"]
    assert a["prefix_sha"] != compute_stamps({"system": blocks[:1]})["prefix_sha"]


def test_responses_api_instructions_are_the_prefix():
    out = compute_stamps({"model": "gpt", "instructions": "You are Rob.",
                          "input": [{"role": "user", "content": "go"}]})
    assert out["prefix_sha"] == compute_stamps(_req(system="You are Rob."))["prefix_sha"]


def test_explicit_system_argument_wins():
    out = compute_stamps({"messages": [{"role": "system", "content": "ignored"}]},
                         system="You are Rob.")
    assert out["prefix_sha"] == compute_stamps(_req(system="You are Rob."))["prefix_sha"]


def test_a_request_with_no_prefix_anywhere_is_still_silent():
    assert compute_stamps({"input": [{"role": "user", "content": "go"}]}) == {}
    assert compute_stamps({"system": ""}) == {}
