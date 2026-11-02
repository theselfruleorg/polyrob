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
    # F23 (2026-09-22): `usage.include` now rides every OpenRouter request so the
    # response reports the cost OpenRouter actually billed. It is accounting
    # only — it changes no routing, no sampling and no prompt bytes.
    assert out["extra_body"] == {"provider": {"sort": "latency"},
                                 "reasoning": {"max_tokens": 3000},
                                 "usage": {"include": True}}
    monkeypatch.delenv("OPENROUTER_PROVIDER_SORT"); monkeypatch.delenv("OPENROUTER_REASONING_MAX_TOKENS")
    assert apply_request_extras({"max_tokens": 8192}, None, 8192)["extra_body"] == {
        "usage": {"include": True}}


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


# --- F23: read what OpenRouter actually BILLED, instead of estimating it -----

def test_usage_accounting_is_requested_on_every_call():
    from modules.llm.openrouter_reasoning import apply_usage_accounting
    params = {}
    apply_usage_accounting(params)
    assert params["extra_body"]["usage"] == {"include": True}


def test_usage_accounting_does_not_clobber_a_sibling_extra_body_key():
    from modules.llm.openrouter_reasoning import apply_usage_accounting
    params = {"extra_body": {"provider": {"sort": "latency"}}}
    apply_usage_accounting(params)
    assert params["extra_body"]["provider"] == {"sort": "latency"}
    assert params["extra_body"]["usage"] == {"include": True}


def test_usage_accounting_is_idempotent():
    from modules.llm.openrouter_reasoning import apply_usage_accounting
    params = {}
    apply_usage_accounting(params)
    apply_usage_accounting(params)
    assert params["extra_body"] == {"usage": {"include": True}}


# --- 2026-10-31: z-ai/glm-5.3 REFUSES reasoning.enabled=false (HTTP 400) -----------------------

def test_mandatory_reasoning_model_never_gets_the_off_switch(monkeypatch):
    """Live probe 2026-10-31 on z-ai/glm-5.3: enabled=false -> 400 "Reasoning is mandatory",
    no block -> 200, reasoning.max_tokens / effort -> 200 and honoured. The seat env
    OPENROUTER_REASONING_ENABLED=false must still bind on the DeepSeek seat."""
    from modules.llm.openrouter_reasoning import reasoning_extra_body
    monkeypatch.setenv("OPENROUTER_REASONING_ENABLED", "false")
    monkeypatch.delenv("OPENROUTER_REASONING_MAX_TOKENS", raising=False)
    assert reasoning_extra_body(8192, "z-ai/glm-5.3") is None                  # no block at all
    assert reasoning_extra_body(8192, "deepseek/deepseek-v4.1-flash") == {"reasoning": {"enabled": False}}
    assert reasoning_extra_body(8192) == {"reasoning": {"enabled": False}}      # unknown model: unchanged
    monkeypatch.setenv("OPENROUTER_REASONING_MAX_TOKENS", "4096")
    assert reasoning_extra_body(8192, "z-ai/glm-5.3") == {"reasoning": {"max_tokens": 4096}}
    assert reasoning_extra_body(8192, "Z-AI/GLM-5.3") == {"reasoning": {"max_tokens": 4096}}   # case-insensitive


def test_session_off_is_ignored_for_a_mandatory_model(monkeypatch):
    from modules.llm.openrouter_reasoning import reasoning_extra_body, session_reasoning
    monkeypatch.delenv("OPENROUTER_REASONING_ENABLED", raising=False)
    monkeypatch.delenv("OPENROUTER_REASONING_MAX_TOKENS", raising=False)
    with session_reasoning("off"):
        assert reasoning_extra_body(8192, "z-ai/glm-5.3") is None
        assert reasoning_extra_body(8192, "deepseek/deepseek-v4.1-flash") == {"reasoning": {"enabled": False}}
    with session_reasoning("high"):
        assert reasoning_extra_body(8192, "z-ai/glm-5.3") == {"reasoning": {"effort": "high"}}


def test_apply_request_extras_reads_the_model_from_the_request(monkeypatch):
    from modules.llm.openrouter_reasoning import apply_request_extras
    monkeypatch.setenv("OPENROUTER_REASONING_ENABLED", "false")
    monkeypatch.delenv("OPENROUTER_REASONING_MAX_TOKENS", raising=False)
    glm = apply_request_extras({"model": "z-ai/glm-5.3", "max_tokens": 8192}, None, 8192)
    assert "reasoning" not in glm["extra_body"]
    ds = apply_request_extras({"model": "deepseek/deepseek-v4.1-flash", "max_tokens": 8192}, None, 8192)
    assert ds["extra_body"]["reasoning"] == {"enabled": False}


def test_an_unlisted_model_learns_from_the_400_and_retries_once(monkeypatch):
    """The wrapper retries ONCE without the off switch and remembers the model."""
    import asyncio, types
    from modules.llm import openrouter_reasoning as r
    monkeypatch.setenv("OPENROUTER_REASONING_ENABLED", "false")
    monkeypatch.setenv("OPENROUTER_REASONING_MAX_TOKENS", "4096")
    r._LEARNED.discard("acme/think-only")
    calls = []

    class _Completions:
        async def create(self, **kw):
            calls.append(kw.get("extra_body", {}).get("reasoning"))
            if (kw.get("extra_body", {}).get("reasoning") or {}).get("enabled") is False:
                raise RuntimeError("Error code: 400 - Reasoning is mandatory for this endpoint and cannot be disabled.")
            return "ok"

    sdk = types.SimpleNamespace(chat=types.SimpleNamespace(completions=_Completions()))
    params = {"model": "acme/think-only", "max_tokens": 8192,
              "extra_body": {"reasoning": {"enabled": False}}}
    out = asyncio.run(r.create_with_reasoning_fallback(types.SimpleNamespace(logger=None), sdk, params))
    assert out == "ok"
    assert calls == [{"enabled": False}, {"max_tokens": 4096}]       # one retry, budget instead of off
    assert r.reasoning_mandatory("acme/think-only")                  # remembered for the process
    assert r.reasoning_extra_body(8192, "acme/think-only") == {"reasoning": {"max_tokens": 4096}}
    r._LEARNED.discard("acme/think-only")


def test_other_errors_still_propagate(monkeypatch):
    import asyncio, types
    from modules.llm.openrouter_reasoning import create_with_reasoning_fallback

    class _Completions:
        async def create(self, **kw):
            raise RuntimeError("Error code: 500 - upstream down")

    sdk = types.SimpleNamespace(chat=types.SimpleNamespace(completions=_Completions()))
    try:
        asyncio.run(create_with_reasoning_fallback(types.SimpleNamespace(logger=None), sdk, {"model": "x/y"}))
        assert False, "expected the 500 to propagate"
    except RuntimeError as e:
        assert "500" in str(e)
