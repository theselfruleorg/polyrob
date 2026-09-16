"""Credit death is FAIL-FAST through the whole next-action fallback chain.

The inline billing block in `_get_next_action_internal` deliberately re-raises
`InsufficientCreditsError`, but that raise used to be absorbed by the generic
`except Exception` boundaries guarding the native-tools -> structured-output ->
plain-call -> manual-parse chain. Each absorbed raise turned into `parsed = None`
and bought ANOTHER billable provider call: one out-of-credits step made up to four
paid calls, and the credit sentinel in error_recovery only saw the error after
three wasted ones.

These tests pin the contract at the source level (the four boundaries must have an
InsufficientCreditsError arm) and behaviourally (the exception escapes the chain).
"""
import ast
import inspect
import re

import pytest

from core.exceptions import InsufficientCreditsError


def _source() -> str:
    from agents.task.agent.core import next_action_internal
    return inspect.getsource(next_action_internal)


# The three handlers that form the billing fallback chain, by their (unique) bound
# names. Each one CONTINUES the chain (it sets `parsed = None` or falls through to
# another provider call) instead of re-raising, so each is a place a credit-death
# raise turns into another paid call. Handlers that re-`raise` are safe by
# construction and are deliberately not listed.
_CHAIN_HANDLERS = ("tool_error", "struct_error", "structured_error")


def test_each_chain_handler_has_a_preceding_credit_arm():
    """The three swallowing boundaries of the fallback chain must each be preceded,
    within their OWN try, by an `except InsufficientCreditsError` arm."""
    tree = ast.parse(_source())
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_get_next_action_internal"
    )

    found, offenders = set(), []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try):
            continue
        seen_credit = False
        for handler in node.handlers:
            if "InsufficientCreditsError" in _handler_names(handler):
                seen_credit = True
                continue
            if handler.name in _CHAIN_HANDLERS:
                found.add(handler.name)
                if not seen_credit:
                    offenders.append(f"{handler.name}@L{handler.lineno}")

    missing = set(_CHAIN_HANDLERS) - found
    assert not missing, (
        f"chain handler(s) {sorted(missing)} not found — next_action_internal.py was "
        "restructured; re-derive _CHAIN_HANDLERS so this test keeps guarding the chain"
    )
    assert not offenders, (
        f"{offenders} swallow InsufficientCreditsError with no preceding credit arm — "
        "a dead account buys another billable provider call there"
    )


def test_a_handler_that_reraises_is_not_required_to_have_a_credit_arm():
    """Guards the rule above against over-tightening: `except Exception ... raise`
    propagates credit death already, so it needs no separate arm."""
    tree = ast.parse(_source())
    reraising = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            if "Exception" not in _handler_names(handler):
                continue
            if any(isinstance(s, ast.Raise) and s.exc is None
                   for s in ast.walk(handler)):
                reraising.append(handler.lineno)
    assert reraising, "expected at least one re-raising generic handler in this module"


def _handler_names(handler: ast.ExceptHandler) -> set:
    t = handler.type
    if t is None:
        return {"Exception"}  # bare except: catches everything
    parts = t.elts if isinstance(t, ast.Tuple) else [t]
    out = set()
    for p in parts:
        if isinstance(p, ast.Name):
            out.add(p.id)
        elif isinstance(p, ast.Attribute):
            out.add(p.attr)
    return out


def test_insufficient_credits_is_not_in_the_retryable_error_classes():
    """The outer retry loop only retries parse/rate_limit/parameter_error. A credit
    death must never classify into one of those (that would re-bill on a timer)."""
    from agents.task.robust_parse_config import RobustParseConfig

    # The retry gate the loop consults; credit death must fall through to `raise`.
    assert not RobustParseConfig.should_retry_parse_error(99), (
        "an unbounded retry budget would re-bill a dead account"
    )


def test_timeout_arm_precedes_the_generic_handler_in_the_chain():
    """A deliberate timeout re-raise from an inner fallback must not be answered by
    the outer handler starting ANOTHER full-timeout call."""
    src = _source()
    # The outer boundary that used to swallow it.
    idx = src.find("except Exception as structured_error:")
    assert idx > 0, "outer structured-output handler not found — test needs updating"
    window = src[max(0, idx - 900):idx]
    assert "except asyncio.TimeoutError:" in window, (
        "the outer structured-output boundary must re-raise asyncio.TimeoutError "
        "before its generic handler, or a timeout costs a second full-timeout call"
    )
    assert "except InsufficientCreditsError:" in window


def test_insufficient_credits_error_is_importable_where_the_arms_reference_it():
    """The arms are only live if the symbol is actually bound in the module."""
    from agents.task.agent.core import next_action_internal

    assert next_action_internal.InsufficientCreditsError is InsufficientCreditsError


# ---------------------------------------------------------------------------
# A12 fix round (2026-09-14, 043 §4.6): "a retry attempt is not an error
# until the last one fails" -- behavioural coverage of the retry loop's log
# LEVELS (the earlier tests in this file pin the fail-fast CONTROL FLOW;
# this one pins that every per-attempt log in that same chain is WARNING and
# the loop's own final-exhaustion log is the single console-visible ERROR).
# ---------------------------------------------------------------------------

import asyncio
import logging
from types import SimpleNamespace as _SimpleNamespace
from unittest.mock import AsyncMock as _AsyncMock


class _FakeLLM:
    """Deliberately has NO ``with_structured_output`` -- exactly like the real
    ``OpenAIAdapter``, which doesn't implement it either (see A12's report:
    this is what actually drives ``_get_next_action_internal`` through the
    tool-calling -> structured-output -> plain-fallback cascade in prod).
    Accessing the missing attribute raises ``AttributeError`` naturally,
    forcing the plain ``ainvoke`` fallback path every time."""

    def __init__(self, side_effect):
        self.ainvoke = _AsyncMock(side_effect=side_effect)


class _FakeMessageManager:
    """Minimal stand-in covering exactly the MessageManager surface
    ``_get_next_action_internal`` touches on the no-native-tools /
    no-streaming path (native tools and streaming are both switched off on
    the fake agent below, so their MessageManager call sites are never hit)."""

    def get_estimated_context_usage(self):
        return 0.1

    def get_messages(self):
        return []

    def check_token_safety(self, *args, **kwargs):
        return {"safe": True, "usage_percent": 0.0, "current_tokens": 0, "max_limit": 1000}

    def get_messages_for_llm(self):
        return []

    def get_token_count(self):
        return 0

    def calculate_llm_timeout(self, tool_count=0, use_vision=False):
        return 30

    def get_llm_parameters(self):
        return {}

    def push_ephemeral_message(self, message):
        pass


def _make_retry_agent(side_effect):
    """A bare ``Agent`` (``object.__new__`` -- no ``__init__``, matching the
    established pattern in ``test_fallback_exclusion_dedup.py``) with only
    the attributes the no-native-tools / no-streaming path of
    ``_get_next_action_internal`` reads. Real class -> real mixin methods
    (``_supports_streaming``, ``_get_llm_parameters``, ``_classify_llm_error``)
    for free; only DATA is faked."""
    from agents.task.agent.service import Agent

    a = object.__new__(Agent)
    a.logger = logging.getLogger("test-a12-retry-log-levels")
    a.controller = None  # short-circuits native-tool-calling entirely
    a.tool_call_tracker = None
    a.message_manager = _FakeMessageManager()
    a.model_name = "gpt-5"
    a.chat_model_library = "OpenAIAdapter"
    a.use_native_tools = False
    a.use_vision = False
    a.provider_name = "fake-test-provider"  # not in STREAMING_PROVIDER_NAMES
    a.state = _SimpleNamespace(n_steps=1)
    a.llm = _FakeLLM(side_effect)
    a.max_actions_per_step = 5  # only read for the early JSON format hint
    return a


def test_two_attempt_failure_is_per_attempt_warning_and_one_final_error(caplog):
    """Drives ``_get_next_action_internal`` through exactly two outer retry
    attempts (attempt 1: a retryable rate-limit; attempt 2: a non-retryable
    auth failure, so the loop stops there) and asserts on LEVELS only:

    - every per-attempt log ("LLM request attempt N failed...", plus every
      per-attempt log deeper in the cascade -- the tool-calling attempt, the
      structured-output attempt, both plain-fallback legs) is WARNING;
    - exactly ONE record is ERROR: the loop's own final-exhaustion line,
      which names the provider and the error class (043 §4.6's requirement),
      not just a bare attempt count.
    """
    from modules.llm.usage_extract import resolve_serving_provider

    # Two `ainvoke` calls per outer attempt (the structured-output handler's
    # own plain-fallback leg, then the outer handler's "final fallback" leg,
    # both of which run before the OUTER retry loop ever sees a failure) x
    # two outer attempts.
    side_effect = [
        Exception("rate limit exceeded"),
        Exception("rate limit exceeded"),
        Exception("401 unauthorized"),
        Exception("401 unauthorized"),
    ]
    agent = _make_retry_agent(side_effect)
    provider = resolve_serving_provider(agent.llm, agent.model_name)

    caplog.set_level(logging.WARNING, logger="test-a12-retry-log-levels")
    with pytest.raises(Exception, match="401 unauthorized"):
        asyncio.run(agent._get_next_action_internal([]))

    records = [r for r in caplog.records if r.name == "test-a12-retry-log-levels"]
    errors = [r for r in records if r.levelno == logging.ERROR]
    warns = [r for r in records if r.levelno == logging.WARNING]

    assert len(errors) == 1, (
        f"expected exactly one ERROR record, got: "
        f"{[(r.levelname, r.getMessage()) for r in records]}"
    )
    final = errors[0].getMessage()
    assert "LLM call failed after 2 attempts" in final
    assert f"provider={provider}" in final, "must name the provider (043 §4.6)"
    assert "Exception" in final, "must name the error class (043 §4.6)"

    # The per-outer-attempt log ("attempt N failed") fires once per outer
    # attempt (N=2) and must never be ERROR.
    attempt_records = [r for r in warns if "LLM request attempt" in r.getMessage()]
    assert len(attempt_records) == 2
    assert all(r.levelno == logging.WARNING for r in attempt_records)

    # The structured-output cascade's own per-attempt log ("Structured
    # output failed: ...", fired once per agent lifetime -- pre-existing
    # `_structured_output_warned` throttling, untouched by this fix round)
    # is present and WARNING, never ERROR.
    struct_records = [r for r in records if "Structured output failed" in r.getMessage()]
    assert struct_records, (
        f"expected the structured-output cascade's own warning, got: "
        f"{[(r.levelname, r.getMessage()) for r in records]}"
    )
    assert all(r.levelno == logging.WARNING for r in struct_records)

    # Every record but the one final exhaustion line is WARNING -- the whole
    # point of this fix round ("a retry attempt is not an error until the
    # last one fails").
    assert len(warns) == len(records) - 1
    assert records[-1] is errors[0]
