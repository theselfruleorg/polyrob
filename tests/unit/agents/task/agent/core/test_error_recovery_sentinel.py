"""Task 10 (§6.3 follow-up): ONE universal credit-sentinel trip site.

The 2026-07-16 outage: an owner asked a question in chat, the LLM returned
HTTP 402, the session failed — and nothing tripped the provider-credit
sentinel, because the only two trip sites (cron/runner.py, goals/dispatcher.py)
are both BACKGROUND paths. `error_recovery.py::_handle_step_error` is the
universal LLM-error path (chat, goals, cron, sub-agents all flow through it),
so the trip now lives there — closing the interactive gap and replacing the
two partial call sites with one.

Interactive deliberately does NOT check the latch (`credit_sentinel_active()`)
before trying: an owner-initiated turn always attempts the call, since the
owner may have just topped up. Only the CHECK sites in cron/runner.py and
goals/dispatcher.py are unaffected by this change.
"""
import logging

import pytest
from unittest.mock import AsyncMock, MagicMock

from agents.task.agent.service import Agent
from core.exceptions import (
    LLMContextLengthError,
    LLMError,
    LLMPermanentError,
    LLMProviderExhaustedError,
)


# The REAL text from the 2026-07-16 prod journal, verbatim. Note what it does NOT
# contain: "insufficient_quota", "billing", "invalid_api_key", "account_deactivated".
# It only says "402" and "credits" — which `looks_like_credit_death` matches and the
# `is_permanent` disjunction does not.
_REAL_402_TEXT = (
    "Failed to generate agent response: OpenRouter generation failed: "
    "Error code: 402 - {'error': {'message': 'This request requires more credits, "
    "or fewer max_tokens. You requested up to 16384 tokens, but can only afford "
    "1591. To increase, visit https://openrouter.ai/settings/credits and add more "
    "credits', 'code': 402}}"
)


def _real_openrouter_402_error():
    """The real prod shape (2026-07-16 outage), with the real EXCEPTION TYPE.

    This is a plain `LLMError`, NOT `LLMPermanentError`. Two independent reasons a
    real 402 never arrives typed as permanent (both verified 2026-07-16):

    1. `llm_client.py::translate_llm_error` does map '402' → LLMPermanentError, but
       it is never called on the tool-calling path. `OpenRouterClient.
       generate_agent_response` catches the raw provider exception and re-wraps it
       as a generic `LLMError` ("Failed to generate agent response: ..."), and the
       step loop ALWAYS calls with `tools=` (next_action_internal.py:627) — so this
       is the DEFAULT path, not an edge case. Anthropic/OpenAI clients do the same.
    2. Even a correctly-typed LLMPermanentError would not survive: `llm_runner.py::
       get_next_action` catches generic `LLMError` (which LLMPermanentError IS-A,
       with no earlier `except LLMPermanentError`) and re-raises
       `LLMProviderExhaustedError` on fallback failure.

    The previous version of this helper asserted the opposite (LLMPermanentError,
    "translate_llm_error matches 402 before the raw exception gets here") — that
    fabricated shape is exactly why the unreachable trip site tested green.
    """
    return LLMError(_REAL_402_TEXT)


def _make_agent(role="orchestrator", is_sub_agent=False):
    """Mirrors tests/unit/agents/test_error_recovery_routing.py::_build_agent —
    a bare Agent double built via object.__new__ with just the attributes
    `_handle_step_error` touches."""
    a = object.__new__(Agent)
    a.logger = logging.getLogger("test_error_recovery_sentinel")
    a.max_failures = 5
    a.retry_delay = 0  # real attr; 0 keeps the retry-backoff sleep out of the tests

    st = MagicMock()
    st.consecutive_failures = 0
    st.stopped = False
    # Faithful defaults: a bare MagicMock returns a TRUTHY mock from
    # track_llm_error(), which spuriously trips the circuit breaker and makes
    # _handle_step_error raise LLMProviderExhaustedError instead of taking the
    # branch under test. Pin them to the real first-failure values.
    st.track_llm_error = MagicMock(return_value=False)  # circuit not tripped
    st.llm_providers_failed = set()
    a.state = st

    mm = MagicMock()
    a.message_manager = mm
    a.model_name = "gpt-4o"

    a.controller = MagicMock()
    a.telemetry_manager = MagicMock()

    a._recover_from_error = AsyncMock()
    a._get_provider_from_model = MagicMock(return_value="openai")
    a._attempt_llm_fallback_in_handler = AsyncMock(return_value=False)

    orch = MagicMock()
    orch.container = MagicMock()
    orch.user_id = "u1"
    a.orchestrator = orch
    a._role = role
    a._is_sub_agent = is_sub_agent
    return a


@pytest.fixture()
def spy_trip(monkeypatch):
    """Record every trip_credit_sentinel call. error_recovery imports the symbol
    lazily inside the function, so patching the module attribute is picked up."""
    tripped = []

    async def fake_trip(reason, *, container=None, user_id=None):
        tripped.append(reason)
        return True

    monkeypatch.setattr("core.credit_sentinel.trip_credit_sentinel", fake_trip)
    return tripped


@pytest.mark.asyncio
async def test_real_prod_402_as_plain_llmerror_trips_sentinel(spy_trip):
    """THE regression test for the 2026-07-16 outage.

    The owner asked a question, OpenRouter was out of credits, the session died
    and the owner got total silence. The trip added in Task 10 could not fire:
    it was nested inside `if is_permanent:`, and this error — a plain `LLMError`
    whose text has "402"/"credits" but none of the is_permanent markers —
    satisfies NONE of that disjunction. The credit-death classifier
    (`looks_like_credit_death`, which DOES match this) must not be gated behind
    the narrower permanent-error classifier that doesn't.
    """
    agent = _make_agent(role="orchestrator", is_sub_agent=False)  # interactive
    await agent._handle_step_error(_real_openrouter_402_error())
    assert spy_trip, (
        "the REAL prod 402 (plain LLMError, no insufficient_quota/billing marker) "
        "must trip the credit sentinel"
    )
    assert "402" in spy_trip[0]


@pytest.mark.asyncio
async def test_provider_exhausted_with_credit_death_trips_sentinel(spy_trip):
    """The branch a real fallback-exhausted 402 actually lands in.

    `llm_runner.get_next_action` catches generic LLMError and re-raises
    LLMProviderExhaustedError when fallback fails, so this — not the is_permanent
    branch — is where a credit-dead prod session ends up. That branch halts and
    returns early, so a trip nested in any later branch is dead code for it.
    """
    agent = _make_agent()
    err = LLMProviderExhaustedError(
        f"Primary LLM failed: {_REAL_402_TEXT}. Fallback also failed: {_REAL_402_TEXT}",
        providers_tried=["openrouter", "openai"],
    )
    result = await agent._handle_step_error(err)

    assert spy_trip, "provider-exhausted credit death must trip the credit sentinel"
    # The trip is a side-effect only — it must not alter which branch handles the error.
    assert agent.state.stopped is True
    assert result and "All LLM providers failed" in result[0].error


@pytest.mark.asyncio
async def test_permanent_error_credit_death_still_trips_sentinel(spy_trip):
    """LLMPermanentError keeps tripping — the fix widens reach, never narrows it."""
    agent = _make_agent()
    await agent._handle_step_error(
        LLMPermanentError("Error code: 402 - insufficient_quota")
    )
    assert spy_trip


@pytest.mark.asyncio
async def test_trip_fires_exactly_once_per_error(spy_trip):
    """One trip site, one trip — no double-trip across branches. (Belt and braces:
    trip_credit_sentinel is itself idempotent while the latch is active, but that
    must not be what saves us from a duplicated call site.)"""
    agent = _make_agent()
    await agent._handle_step_error(_real_openrouter_402_error())
    assert len(spy_trip) == 1, f"expected exactly one trip, got {len(spy_trip)}"


@pytest.mark.asyncio
async def test_non_billing_error_never_trips(spy_trip):
    """Widening the trip must not make it fire on ordinary failures."""
    agent = _make_agent()
    await agent._handle_step_error(ValueError("some parsing problem"))
    assert spy_trip == []


# === Final whole-branch review, Finding 2: false-positive surface ===
# `_trip_sentinel_if_credit_death` ran on `str(error)` for EVERY exception
# reaching `_handle_step_error` (step.py's `except Exception` catches ANY step
# failure, not just LLM errors), and `_CREDIT_DEATH_MARKERS` matches a bare
# "402"/"billing" substring. A long session that merely overflows context, or
# a tool/parse crash whose text happens to contain those substrings, latched
# the sentinel and paused autonomy for CREDIT_SENTINEL_RELEASE_HOURS (6h) with
# nothing actually wrong with billing.

@pytest.mark.asyncio
async def test_context_overflow_carrying_credit_marker_never_trips(spy_trip):
    """A context-length overflow that happens to mention a token count
    containing "402" (e.g. "requested 130402 tokens") must never trip the
    sentinel — it needs a smaller prompt, not a paused autonomy loop. This is
    ALSO handled structurally: LLMContextLengthError early-returns (compact +
    retry) before the trip site is reached at all."""
    agent = _make_agent()
    err = LLMContextLengthError(
        "Context length exceeded: however you requested 130402 tokens, the "
        "model's maximum context length is 128000 tokens.")
    result = await agent._handle_step_error(err)
    assert spy_trip == [], "context overflow must never trip the credit sentinel"
    assert result == []  # the existing compact-and-retry contract, unchanged


@pytest.mark.asyncio
async def test_non_llm_error_with_hex_request_id_never_trips(spy_trip):
    """A non-LLM exception (e.g. a tool/HTTP failure) whose text happens to
    embed a hex request id containing the digits "402" (a coincidence, not a
    billing signal) must not trip — the trip is now type-gated to the LLM
    exception family, so a tool/browser/file/parse error can never reach
    `looks_like_credit_death` regardless of what substrings its text contains."""
    agent = _make_agent()
    err = RuntimeError("upstream call failed (request_id: req_9f402ab13c7e)")
    await agent._handle_step_error(err)
    assert spy_trip == [], "a coincidental '402' substring in a non-LLM error must not trip"


@pytest.mark.asyncio
async def test_nameerror_mentioning_billing_never_trips(spy_trip):
    """A plain Python NameError (a real bug, not a provider refusal) whose
    message happens to mention a variable named 'billing_total' must not trip
    the sentinel — type-gating excludes it from ever reaching the classifier."""
    agent = _make_agent()
    err = NameError("name 'billing_total' is not defined")
    await agent._handle_step_error(err)
    assert spy_trip == [], "a NameError must never trip the credit sentinel"


@pytest.mark.asyncio
async def test_trip_failure_is_fail_open_and_does_not_mask_original_error(monkeypatch):
    """A broken sentinel must never break error recovery or hide the real
    PERMANENT ERROR result the caller (and eventually the owner) needs to see."""

    async def broken_trip(reason, *, container=None, user_id=None):
        raise RuntimeError("sentinel latch write exploded")

    monkeypatch.setattr("core.credit_sentinel.trip_credit_sentinel", broken_trip)

    agent = _make_agent()
    err = Exception("Error code: 402 - insufficient_quota")
    result = await agent._handle_step_error(err)

    assert agent.state.stopped is True
    assert result and "PERMANENT ERROR" in result[0].error


@pytest.mark.asyncio
async def test_fail_open_on_real_402_shape_preserves_llm_error_routing(monkeypatch):
    """Fail-open on the REAL shape too: a raising sentinel must not break the
    is_llm_error path this error actually takes, nor mask the original error."""

    async def broken_trip(reason, *, container=None, user_id=None):
        raise RuntimeError("sentinel latch write exploded")

    monkeypatch.setattr("core.credit_sentinel.trip_credit_sentinel", broken_trip)

    agent = _make_agent()
    result = await agent._handle_step_error(_real_openrouter_402_error())

    assert result, "recovery must still return a result the caller can surface"
    assert "402" in result[0].error, "original error must not be masked"


@pytest.mark.asyncio
async def test_integration_real_agent_credit_death_latches_real_sentinel(
        tmp_path, monkeypatch):
    """END-TO-END: a real Agent._handle_step_error → real trip_credit_sentinel →
    real on-disk latch. Nothing about the sentinel is mocked here; only the
    outbound delivery rail is stubbed (and asserted), so this test fails if the
    trip is unreachable, the latch write breaks, or the notice stops going out.

    This replaces `tests/unit/core/test_credit_sentinel.py::
    test_dispatcher_refusal_trips_sentinel_on_credit_death`, which Task 10 DELETED
    when it moved the trip out of GoalDispatcher. The behaviour ("a credit-death
    failure latches the sentinel") still exists — it just moved — so the test
    should have been rewritten against the new site, not dropped. Deleting it
    removed the only end-to-end assertion on the real latch and left the
    replacement covered solely by tests that mocked trip_credit_sentinel out;
    with the trip nested under `is_permanent` those mocks were fed a fabricated
    LLMPermanentError and passed while the real path could never fire. Had this
    test existed, it would have caught that Critical.
    """
    import core.credit_sentinel as cs

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CREDIT_SENTINEL_ENABLED", "true")

    notices = []

    async def _fake_deliver(container, user_id, text, **kw):
        notices.append(text)
        return "sent"

    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", _fake_deliver)

    assert cs.credit_sentinel_active() is False, "latch must start clear"

    agent = _make_agent(role="orchestrator", is_sub_agent=False)  # interactive owner turn
    await agent._handle_step_error(_real_openrouter_402_error())

    assert cs.credit_sentinel_active() is True, (
        "the real prod 402 through a real Agent must latch the real credit sentinel"
    )
    assert len(notices) == 1, "exactly one owner-facing safety-net notice"
    assert "402" in notices[0]


@pytest.mark.asyncio
async def test_background_check_sites_unchanged_interactive_does_not_check(monkeypatch):
    """Background paths (cron/goals) still gate on credit_sentinel_active() at
    their own call sites — untouched by this change. Interactive has no such
    check: _handle_step_error always attempts the billing branch regardless of
    latch state (it doesn't even consult credit_sentinel_active()).

    Uses a real `LLMError` (not a bare `Exception`) — final review Finding 2
    type-gated the trip to the LLM exception family, and no real billing error
    ever arrives as a bare `Exception` in production (see
    `_real_openrouter_402_error`'s docstring); a bare `Exception` would no
    longer reach `looks_like_credit_death` at all, which would make this test
    assert nothing about the code path it's named for.
    """
    import core.credit_sentinel as cs

    checked = []
    real_active = cs.credit_sentinel_active

    def spy_active():
        checked.append(True)
        return real_active()

    monkeypatch.setattr("core.credit_sentinel.credit_sentinel_active", spy_active)

    tripped = []

    async def fake_trip(reason, *, container=None, user_id=None):
        tripped.append(reason)

    monkeypatch.setattr("core.credit_sentinel.trip_credit_sentinel", fake_trip)

    agent = _make_agent(role="orchestrator", is_sub_agent=False)
    err = LLMError("Error code: 402 - insufficient_quota")
    await agent._handle_step_error(err)

    assert tripped, "interactive billing error still trips"
    assert not checked, "interactive path must not consult credit_sentinel_active()"
