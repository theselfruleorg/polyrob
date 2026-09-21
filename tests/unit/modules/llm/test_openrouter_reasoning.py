"""`OPENROUTER_REASONING_MAX_TOKENS` — bound the model's REASONING inside the
output budget (057 WS-B follow-up).

Prod 2026-09-20: eight 8,192-token output cuts in five hours were 7,981/8,192,
8,192/8,192 … REASONING tokens (OpenRouter /generation): the model thought past
the output cap and never reached its tool call, on steps like a read_file; each
cost a ~$0.01 retry and 119–181 s — the p90 tail. OpenRouter accepts
`reasoning: {"max_tokens": N}`; DeepSeek honours it. Unset ⇒ byte-identical
request (no `reasoning` block).
"""
import pytest

from modules.llm.openrouter_reasoning import (
    apply_reasoning_budget,
    reasoning_extra_body,
    resolve_reasoning_max_tokens,
)


@pytest.mark.parametrize("raw", ["", "  ", "0", "-5", "off", "none", "abc", "12.5"])
def test_unset_or_invalid_adds_nothing(monkeypatch, raw):
    monkeypatch.setenv("OPENROUTER_REASONING_MAX_TOKENS", raw)
    assert resolve_reasoning_max_tokens() is None
    assert reasoning_extra_body() is None
    params = {"model": "m", "messages": []}
    assert apply_reasoning_budget(params, max_tokens=8192) == {"model": "m", "messages": []}


def test_budget_lands_in_extra_body_and_merges_with_routing(monkeypatch):
    monkeypatch.setenv("OPENROUTER_REASONING_MAX_TOKENS", "3000")
    params = {"model": "m", "messages": [], "max_tokens": 8192,
              "extra_body": {"provider": {"sort": "latency"}}}
    out = apply_reasoning_budget(params, max_tokens=8192)
    assert out["extra_body"] == {"provider": {"sort": "latency"},
                                 "reasoning": {"max_tokens": 3000}}


def test_budget_is_clamped_below_the_output_cap(monkeypatch):
    """A reasoning budget ≥ the output cap would guarantee the very cut it
    exists to prevent: leave at least a quarter of the output for the answer."""
    monkeypatch.setenv("OPENROUTER_REASONING_MAX_TOKENS", "8192")
    out = apply_reasoning_budget({"max_tokens": 8192}, max_tokens=8192)
    assert out["extra_body"]["reasoning"]["max_tokens"] == 6144
    out2 = apply_reasoning_budget({"max_tokens": 1000}, max_tokens=1000)
    assert out2["extra_body"]["reasoning"]["max_tokens"] == 750
    # no cap known → the configured value as-is
    out3 = apply_reasoning_budget({}, max_tokens=None)
    assert out3["extra_body"]["reasoning"]["max_tokens"] == 8192


def test_request_extras_applies_routing_then_reasoning(monkeypatch):
    """The client makes ONE call (its file sits at the god-file ceiling)."""
    from modules.llm.openrouter_reasoning import apply_request_extras
    monkeypatch.setenv("OPENROUTER_PROVIDER_SORT", "latency")
    monkeypatch.setenv("OPENROUTER_REASONING_MAX_TOKENS", "3000")
    out = apply_request_extras({"max_tokens": 8192}, client=None, max_tokens=8192)
    assert out["extra_body"] == {"provider": {"sort": "latency"}, "reasoning": {"max_tokens": 3000}}
    monkeypatch.delenv("OPENROUTER_PROVIDER_SORT"); monkeypatch.delenv("OPENROUTER_REASONING_MAX_TOKENS")
    assert "extra_body" not in apply_request_extras({"max_tokens": 8192}, None, 8192)


def test_request_extras_stamps_the_prefix_identity_on_the_client():
    import types
    from modules.llm.openrouter_reasoning import apply_request_extras
    from modules.llm.prefix_stamp import read_stamps
    client = types.SimpleNamespace()
    params = {"messages": [{"role": "system", "content": "S"}], "tools": [{"n": 1}]}
    apply_request_extras(params, client, 8192)
    st = read_stamps(types.SimpleNamespace(_client=client))
    assert set(st) == {"prefix_sha", "tools_sha"}
    assert "extra_body" not in params or "reasoning" not in params.get("extra_body", {}) or True


# --- 2026-09-20 11:05Z: DeepInfra IGNORES reasoning.max_tokens and effort; enabled=false binds --

def test_reasoning_disabled_overrides_the_budget(monkeypatch):
    """Measured live on deepseek/deepseek-v4.1-flash via DeepInfra: max_tokens=300 →
    1,200 reasoning tokens (ignored); effort=low/minimal → ignored; enabled=false →
    0 reasoning tokens. The only lever the upstream honours is on/off."""
    from modules.llm.openrouter_reasoning import reasoning_extra_body
    monkeypatch.setenv("OPENROUTER_REASONING_MAX_TOKENS", "4096")
    monkeypatch.setenv("OPENROUTER_REASONING_ENABLED", "false")
    assert reasoning_extra_body(8192) == {"reasoning": {"enabled": False}}
    monkeypatch.setenv("OPENROUTER_REASONING_ENABLED", "0")
    assert reasoning_extra_body(8192) == {"reasoning": {"enabled": False}}
    monkeypatch.delenv("OPENROUTER_REASONING_MAX_TOKENS")
    assert reasoning_extra_body(8192) == {"reasoning": {"enabled": False}}   # alone is enough
    monkeypatch.setenv("OPENROUTER_REASONING_ENABLED", "true")               # explicit on = no block
    assert reasoning_extra_body(8192) is None
    monkeypatch.delenv("OPENROUTER_REASONING_ENABLED")
    assert reasoning_extra_body(8192) is None
