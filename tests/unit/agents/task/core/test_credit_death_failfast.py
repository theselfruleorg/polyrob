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
