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


def test_no_handler_in_the_call_path_swallows_a_failed_call():
    """A failed provider call propagates. A handler that catches everything and does not
    re-raise is a place where credit death turns into another paid call."""
    tree = ast.parse(_source())
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_get_next_action_internal"
    )
    assert "with_structured_output" not in ast.dump(fn)
    for node in ast.walk(fn):
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                if handler.name in ("tool_error", "struct_error", "structured_error"):
                    raise AssertionError(f"fallback chain handler {handler.name} is back")


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
    """The outer retry loop only retries parse/rate_limit. A credit
    death must never classify into one of those (that would re-bill on a timer)."""
    from agents.task.robust_parse_config import RobustParseConfig

    # The retry gate the loop consults; credit death must fall through to `raise`.
    assert not RobustParseConfig.should_retry_parse_error(99), (
        "an unbounded retry budget would re-bill a dead account"
    )


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
    """The plain ``ainvoke`` path: the controller below is None, so no native tools."""

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
    a.provider_name = "fake-test-provider"  # no spec row, so no streaming
    a.state = _SimpleNamespace(n_steps=1)
    a.llm = _FakeLLM(side_effect)
    a.max_actions_per_step = 5  # only read for the early JSON format hint
    return a


def test_two_attempt_failure_is_per_attempt_warning_and_one_final_error(caplog):
    """Drives ``_get_next_action_internal`` through exactly two outer retry
    attempts (attempt 1: a retryable rate-limit; attempt 2: a non-retryable
    auth failure, so the loop stops there) and asserts on LEVELS only:

    - every per-attempt log ("LLM request attempt N failed...", plus every
      per-attempt log deeper in the cascade) is WARNING;
    - exactly ONE record is ERROR: the loop's own final-exhaustion line,
      which names the provider and the error class (043 §4.6's requirement),
      not just a bare attempt count.
    """
    from modules.llm.usage_extract import resolve_serving_provider

    # One `ainvoke` call per outer attempt.
    side_effect = [
        Exception("rate limit exceeded"),
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

    assert agent.llm.ainvoke.await_count == 2, "each attempt makes one call"

    # Every record but the one final exhaustion line is WARNING -- the whole
    # point of this fix round ("a retry attempt is not an error until the
    # last one fails").
    assert len(warns) == len(records) - 1
    assert records[-1] is errors[0]
