"""Sampling params go where the INSTALLED Anthropic SDK can actually carry them.

⚠️ The 2026-09-24 outage. With the OpenRouter seat latched by the credit
sentinel, Rob's fallback reached `zai-coding` — an Anthropic-PROTOCOL seat — and
every call died before it left the process:

    TypeError: AsyncMessages.create() got an unexpected keyword argument 'temperature'

The pinned SDK (anthropic 1.6.0) dropped `temperature`, `top_p` and `top_k` from
`messages.create()`; sampling moved to `output_config.effort`. Our param builders
have always put `temperature` in the top-level kwargs, so the whole
Anthropic-protocol family — real Anthropic, zai-coding, any compatible endpoint —
was one call away from a TypeError. It stayed latent only because the seat moved
to OpenRouter on 2026-09-16 and nothing exercised the path.

The fix is NOT "drop temperature", because a third-party Anthropic-compatible
endpoint still honours it on the wire; dropping it would silently re-sample every
one of Rob's turns. Nor is it "always smuggle it in `extra_body`", because
against the canonical API an unknown body field is a 400 rather than a default.
So: drop it for `api.anthropic.com`, carry it in `extra_body` for everyone else,
and decide which by asking the SDK function we are ABOUT TO CALL what it accepts
— not by pinning a version number that the next upgrade invalidates.
"""
import pytest

from modules.llm import anthropic_sampling as s


def _new_sdk(*, max_tokens, messages, model, stop_sequences=None,
             extra_body=None):
    """Stands in for anthropic>=1.6 `AsyncMessages.create` — no sampling params.

    Keyword-only and NO `**kwargs`, which is how the real generated method is
    shaped (checked against 1.6.0); that is exactly why it can TypeError.
    """


def _old_sdk(*, max_tokens, messages, model, temperature=None, top_p=None,
             top_k=None, stop_sequences=None, extra_body=None):
    """Stands in for the older signature that still took them."""


def _permissive(**params):
    """Stands in for this repo's many `async def create(self, **params)` stubs."""


ANTHROPIC = "https://api.anthropic.com"
ZAI = "https://api.z.ai/api/anthropic"


def _params(**over):
    base = {"model": "glm-5.3-flash", "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.7}
    base.update(over)
    return base


# --- what the SDK accepts -------------------------------------------------- #

def test_it_asks_the_function_it_is_about_to_call():
    assert "temperature" in s.accepted_params(_old_sdk)
    assert "temperature" not in s.accepted_params(_new_sdk)


def test_an_uninspectable_callable_is_treated_as_accepting_nothing_extra():
    """A C-implemented or heavily wrapped callable must not crash the call path.
    Unknown means we do NOT pass the risky kwarg, which fails safe."""
    class _Uninspectable:
        def __call__(self, **kw):  # pragma: no cover - never invoked
            pass

        @property
        def __signature__(self):
            raise ValueError("no signature for this callable")

    assert s.accepted_params(_Uninspectable()) == frozenset()


def test_a_callable_with_kwargs_accepts_everything():
    """The rule that keeps this fix from rewriting the test tree: a stub that
    swallows `**params` cannot raise TypeError, so nothing needs rerouting."""
    assert s.accepts_any_kwarg(_permissive) is True
    assert s.accepts_any_kwarg(_new_sdk) is False


def test_nothing_moves_for_a_permissive_stub():
    out = s.apply_sampling_params(_params(), create_fn=_permissive, base_url=ZAI)
    assert out["temperature"] == 0.7
    assert "extra_body" not in out


# --- the old SDK: leave everything alone ----------------------------------- #

def test_nothing_moves_when_the_sdk_still_takes_it():
    out = s.apply_sampling_params(_params(), create_fn=_old_sdk, base_url=ZAI)
    assert out["temperature"] == 0.7
    assert "extra_body" not in out


# --- the new SDK against a third-party endpoint: carry it on the wire ------- #

def test_temperature_moves_into_extra_body_for_a_compatible_endpoint():
    out = s.apply_sampling_params(_params(), create_fn=_new_sdk, base_url=ZAI)
    assert "temperature" not in out, "this is the TypeError"
    assert out["extra_body"]["temperature"] == 0.7


def test_top_p_and_top_k_travel_the_same_road():
    out = s.apply_sampling_params(_params(top_p=0.9, top_k=40),
                                  create_fn=_new_sdk, base_url=ZAI)
    assert not {"top_p", "top_k"} & set(out)
    assert out["extra_body"] == {"temperature": 0.7, "top_p": 0.9, "top_k": 40}


def test_an_existing_extra_body_is_merged_not_replaced():
    out = s.apply_sampling_params(_params(extra_body={"thinking_budget": 1}),
                                  create_fn=_new_sdk, base_url=ZAI)
    assert out["extra_body"] == {"thinking_budget": 1, "temperature": 0.7}


def test_a_caller_supplied_extra_body_value_wins():
    """An explicit `extra_body` is the caller saying what goes on the wire; the
    rescued kwarg must not overwrite it."""
    out = s.apply_sampling_params(_params(extra_body={"temperature": 0.1}),
                                  create_fn=_new_sdk, base_url=ZAI)
    assert out["extra_body"]["temperature"] == 0.1


# --- the new SDK against the canonical endpoint: drop it ------------------- #

@pytest.mark.parametrize("url", [ANTHROPIC, "https://api.anthropic.com/v1",
                                 "https://API.Anthropic.com"])
def test_it_is_dropped_for_the_canonical_api_not_smuggled(url):
    """The canonical API removed the field. Sending it anyway turns a param we
    no longer control into a 400 on every call — worse than the default."""
    out = s.apply_sampling_params(_params(), create_fn=_new_sdk, base_url=url)
    assert "temperature" not in out
    assert "extra_body" not in out


def test_an_unknown_base_url_is_treated_as_third_party():
    """Only api.anthropic.com is known to have removed it. Everything else is a
    compatible endpoint until proven otherwise."""
    out = s.apply_sampling_params(_params(), create_fn=_new_sdk,
                                  base_url="https://llm.example.internal")
    assert out["extra_body"]["temperature"] == 0.7


def test_a_missing_base_url_is_treated_as_canonical():
    """No base_url means the SDK default, which IS api.anthropic.com."""
    out = s.apply_sampling_params(_params(), create_fn=_new_sdk, base_url=None)
    assert "temperature" not in out
    assert "extra_body" not in out


# --- everything else is untouched ------------------------------------------ #

def test_non_sampling_params_are_never_touched():
    p = _params(stop_sequences=["x"], tools=[{"name": "t"}],
                system="s", tool_choice={"type": "auto"})
    out = s.apply_sampling_params(p, create_fn=_new_sdk, base_url=ZAI)
    for key in ("model", "max_tokens", "messages", "stop_sequences",
                "tools", "system", "tool_choice"):
        assert out[key] == p[key]


def test_the_caller_s_dict_is_not_mutated():
    p = _params()
    s.apply_sampling_params(p, create_fn=_new_sdk, base_url=ZAI)
    assert p["temperature"] == 0.7, "a shared params dict is retried on failure"


def test_absent_sampling_params_produce_no_extra_body():
    p = {"model": "m", "max_tokens": 8, "messages": []}
    out = s.apply_sampling_params(p, create_fn=_new_sdk, base_url=ZAI)
    assert out == p


# --- the wiring on the client ---------------------------------------------- #

class _FakeMessages:
    create = staticmethod(_new_sdk)


class _FakeSDKClient:
    messages = _FakeMessages()
    base_url = ZAI


class _NoResources:
    base_url = ZAI

    @property
    def messages(self):
        raise RuntimeError("a stubbed client has no resources")


def _route(client_obj, params):
    """Call the client's routing step without building a real client.

    ⚠️ `modules.llm.anthropic_client` imports `anthropic` at module scope, and
    the SDK lives in a per-provider isolated dep dir that a bare test venv does
    not have on its path — hence the importorskip rather than a stub. The
    seam-level tests above carry the logic; these cover the wiring, and they run
    wherever the SDK is installed (prod, and the deploy import-test).
    """
    from types import SimpleNamespace

    pytest.importorskip("anthropic",
                        reason="SDK lives in the isolated provider dep dir")
    from modules.llm.anthropic_client import AnthropicClient
    return AnthropicClient._route_sampling_params(
        SimpleNamespace(_client=client_obj), params)


def test_the_client_routes_through_the_seam():
    out = _route(_FakeSDKClient(), _params())
    assert "temperature" not in out
    assert out["extra_body"]["temperature"] == 0.7


def test_a_client_that_was_never_initialised_passes_params_through():
    """`_client` is None before `initialize()`; the routing step must not be the
    thing that raises there."""
    out = _route(None, _params())
    assert out["temperature"] == 0.7


def test_a_stubbed_client_without_resources_passes_params_through():
    """Many tests inject a mock with no `.messages`. Failing open keeps this
    fix from rewriting unrelated suites."""
    out = _route(_NoResources(), _params())
    assert out["temperature"] == 0.7
