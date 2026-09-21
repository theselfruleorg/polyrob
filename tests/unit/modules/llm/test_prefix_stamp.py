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
