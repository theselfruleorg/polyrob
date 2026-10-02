"""Tests for Anthropic prompt-cache breakpoint injection (flow-efficiency D4-a).

The system prompt (+ tool defs) is stable across steps within a session, so a
cache_control breakpoint on the last system block lets Anthropic serve the whole
tools+system prefix from cache (~10x cheaper repeated input).
"""

import pytest

from modules.llm.anthropic_client import (
    _apply_conversation_cache,
    _build_cached_system_param,
)


def test_string_system_becomes_cached_block():
    out = _build_cached_system_param("You are a helpful agent.")
    assert out == [
        {"type": "text", "text": "You are a helpful agent.",
         "cache_control": {"type": "ephemeral"}}
    ]


def test_list_system_caches_only_last_block():
    out = _build_cached_system_param([
        {"type": "text", "text": "part one"},
        {"type": "text", "text": "part two"},
    ])
    assert "cache_control" not in out[0]
    assert out[1]["cache_control"] == {"type": "ephemeral"}
    # original text preserved
    assert out[0]["text"] == "part one" and out[1]["text"] == "part two"


def test_falsy_system_returns_none():
    assert _build_cached_system_param("") is None
    assert _build_cached_system_param(None) is None


def test_disabled_via_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_PROMPT_CACHE", "0")
    out = _build_cached_system_param("stable prompt")
    assert out == [{"type": "text", "text": "stable prompt"}]
    assert "cache_control" not in out[0]


def test_does_not_mutate_input_list():
    src = [{"type": "text", "text": "x"}]
    _build_cached_system_param(src)
    assert "cache_control" not in src[0]  # input untouched


# ---------------------------------------------------------------------------
# F4 — the cache TTL by session class.
#
# The contract: an UNSTAMPED client issues the byte-identical pre-F4 request
# (the bare ephemeral marker = the API's own 5-minute window), and only a
# resolved "1h" ever adds a key.
# ---------------------------------------------------------------------------


def test_no_ttl_is_byte_identical():
    """No stamp ⇒ exactly the dicts this file asserted before F4."""
    assert _build_cached_system_param("stable") == [
        {"type": "text", "text": "stable", "cache_control": {"type": "ephemeral"}}
    ]
    assert _apply_conversation_cache([{"role": "user", "content": "hi"}]) == [
        {"role": "user", "content": [
            {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}
        ]}
    ]


def test_five_minutes_is_also_the_bare_marker():
    """'5m' is the API default — expressed by OMITTING the key, not by sending it."""
    out = _build_cached_system_param("stable", ttl="5m")
    assert out[-1]["cache_control"] == {"type": "ephemeral"}


def test_one_hour_ttl_on_system_and_conversation():
    sys_out = _build_cached_system_param("stable", ttl="1h")
    assert sys_out[-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

    msgs = _apply_conversation_cache(
        [{"role": "user", "content": "hi"},
         {"role": "assistant", "content": [{"type": "text", "text": "yo"}]}],
        ttl="1h",
    )
    assert msgs[0]["content"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert msgs[1]["content"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_ttl_markers_are_not_shared_objects():
    """Each breakpoint gets its OWN dict — a shared one mutated downstream would
    silently re-ttl every other breakpoint in the request."""
    msgs = _apply_conversation_cache(
        [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}], ttl="1h"
    )
    first = msgs[0]["content"][-1]["cache_control"]
    second = msgs[1]["content"][-1]["cache_control"]
    assert first == second and first is not second


def test_kill_switch_still_wins_over_ttl(monkeypatch):
    monkeypatch.setenv("LLM_PROMPT_CACHE", "0")
    assert _build_cached_system_param("stable", ttl="1h") == [
        {"type": "text", "text": "stable"}
    ]
    msgs = [{"role": "user", "content": "hi"}]
    assert _apply_conversation_cache(msgs, ttl="1h") is msgs


class _FakeAnthropic:
    """The real client's ttl resolution, without the SDK/config construction."""
    from modules.llm.anthropic_client import AnthropicClient as _Base
    _SUPPORTS_CACHE_TTL = _Base._SUPPORTS_CACHE_TTL
    _cache_ttl = _Base._cache_ttl


class _FakeCompat(_FakeAnthropic):
    from modules.llm.compat_anthropic import AnthropicCompatClient as _Compat
    _SUPPORTS_CACHE_TTL = _Compat._SUPPORTS_CACHE_TTL


def test_real_anthropic_client_reads_the_stamp():
    from modules.llm.cache_hints import apply_cache_ttl
    client = _FakeAnthropic()
    assert client._cache_ttl() is None          # unstamped
    apply_cache_ttl(client, "1h")
    assert client._cache_ttl() == "1h"
    apply_cache_ttl(client, "5m")
    assert client._cache_ttl() is None          # 5m renders as the bare marker


def test_compat_client_never_sends_a_ttl():
    """A third-party /v1/messages validator may 4xx on a key it does not know."""
    from modules.llm.cache_hints import apply_cache_ttl
    client = _FakeCompat()
    apply_cache_ttl(client, "1h")
    assert client._cache_ttl() is None


# ---------------------------------------------------------------------------
# F12 — breakpoint PLACEMENT.
#
# The three conversation markers used to sit on the newest three rows: the bytes
# guaranteed to change next step. Nothing marked the end of the foundation, so
# after any tail rewrite the cache was re-written from the system marker forward.
# ---------------------------------------------------------------------------

from modules.llm.cache_hints import conversation_breakpoints


def _tool_use(name="read_file"):
    return {"role": "assistant",
            "content": [{"type": "tool_use", "id": f"t_{name}", "name": name, "input": {}}]}


def _tool_result(name="read_file"):
    return {"role": "user",
            "content": [{"type": "tool_result", "tool_use_id": f"t_{name}", "content": "ok"}]}


def _wire(foundation_rows: int, transactions: int):
    """A wire list: `foundation_rows` pinned rows, then N completed transactions."""
    msgs = [{"role": "user", "content": f"foundation {i}"} for i in range(foundation_rows)]
    for i in range(transactions):
        msgs.append(_tool_use(f"step{i}"))
        msgs.append(_tool_result(f"step{i}"))
    return msgs


def _marked(messages):
    """Indices of the rows carrying a cache_control marker."""
    out = []
    for i, m in enumerate(messages):
        content = m.get("content")
        if isinstance(content, list) and content and isinstance(content[-1], dict) \
                and "cache_control" in content[-1]:
            out.append(i)
    return out


def test_three_conversation_markers_exactly():
    msgs = _wire(foundation_rows=7, transactions=5)
    marked = _marked(_apply_conversation_cache(msgs, foundation_len=7))
    assert len(marked) == 3, marked


def test_one_marker_sits_on_the_last_foundation_message():
    msgs = _wire(foundation_rows=7, transactions=5)
    marked = _marked(_apply_conversation_cache(msgs, foundation_len=7))
    assert marked[0] == 6  # messages[foundation_len - 1]


def test_the_last_message_is_always_marked():
    for transactions in (0, 1, 2, 5):
        msgs = _wire(foundation_rows=7, transactions=transactions)
        marked = _marked(_apply_conversation_cache(msgs, foundation_len=7))
        assert marked[-1] == len(msgs) - 1, (transactions, marked)


def test_moving_markers_land_on_the_last_two_tool_results():
    msgs = _wire(foundation_rows=7, transactions=5)
    endpoints = [i for i, m in enumerate(msgs)
                 if m["role"] == "user" and isinstance(m["content"], list)
                 and m["content"][0].get("type") == "tool_result"]
    assert len(endpoints) == 5
    marked = _marked(_apply_conversation_cache(msgs, foundation_len=7))
    assert marked[1:] == endpoints[-2:]


def test_a_trailing_state_message_does_not_orphan_the_transactions():
    """The per-step state message is a plain user row AFTER the last tool_result.

    The tail must still be marked (it is the newest byte), and the other moving
    marker stays on a settled transaction endpoint.
    """
    msgs = _wire(foundation_rows=7, transactions=5)
    msgs.append({"role": "user", "content": "state: step 6/50"})
    marked = _marked(_apply_conversation_cache(msgs, foundation_len=7))
    assert marked[0] == 6
    assert marked[-1] == len(msgs) - 1
    assert msgs[marked[1]]["content"][0]["type"] == "tool_result"


def test_no_foundation_len_is_byte_identical_to_the_old_last_three():
    msgs = _wire(foundation_rows=4, transactions=3)
    assert conversation_breakpoints(msgs, foundation_len=None) == \
        list(range(len(msgs) - 3, len(msgs)))

    def _legacy(messages, n=3, ttl=None):
        """The pre-F12 body, verbatim."""
        from modules.llm.cache_hints import cache_control_marker
        marker = cache_control_marker(ttl)
        out = [dict(m) for m in messages]
        for m in out[-n:]:
            content = m.get("content")
            if isinstance(content, str):
                m["content"] = [{"type": "text", "text": content,
                                 "cache_control": dict(marker)}]
            elif isinstance(content, list) and content:
                content = [dict(b) if isinstance(b, dict) else b for b in content]
                if isinstance(content[-1], dict):
                    content[-1] = {**content[-1], "cache_control": dict(marker)}
                m["content"] = content
        return out

    assert _apply_conversation_cache(msgs) == _legacy(msgs)
    assert _apply_conversation_cache(msgs, ttl="1h") == _legacy(msgs, ttl="1h")


def test_breakpoints_are_distinct_and_within_budget():
    # foundation longer than the whole list: clamp, never index past the end
    msgs = _wire(foundation_rows=2, transactions=0)
    idx = conversation_breakpoints(msgs, foundation_len=99)
    assert idx == sorted(set(idx)) and len(idx) <= 3
    assert all(0 <= i < len(msgs) for i in idx)
    # empty list
    assert conversation_breakpoints([], foundation_len=4) == []


def test_marker_a_index_is_stable_across_consecutive_steps():
    """PrefixCacheProbe-style: the foundation marker must not move.

    A marker that moves every step is a marker that never gets a cache hit —
    that was the whole F12 defect. Grow the conversation by one transaction per
    step and assert both the INDEX and the marked row's bytes hold.
    """
    from tests.support.prefix_cache import serialize_messages

    foundation_len = 7
    first_seen = None
    previous = None
    for transactions in range(1, 6):
        msgs = _wire(foundation_rows=foundation_len, transactions=transactions)
        out = _apply_conversation_cache(msgs, foundation_len=foundation_len)
        marked = _marked(out)
        if first_seen is None:
            first_seen = marked[0]
        assert marked[0] == first_seen == foundation_len - 1
        serialized = serialize_messages(
            [type("M", (), {"type": m["role"], "content": m["content"]})()
             for m in out[:foundation_len]])
        if previous is not None:
            assert serialized == previous, "the marked foundation bytes moved"
        previous = serialized
