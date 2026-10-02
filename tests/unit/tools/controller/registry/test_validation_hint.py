"""A validation hint describes THIS call's args, never the schema in the abstract.

Live 2026-09-01 21:03:52 and 21:04:30 UTC: `twitter_get_timeline` was called with
`{'max_results': 3, 'user': 'tmachinroBot'}`, failed a `greater_than_equal`
constraint on `max_results`, and the hint appended "Required: ['user']" — the
schema's whole required list. `user` had been supplied, so the agent read it as a
missing field, re-sent the identical call and failed again 38 seconds later.

These tests pin the property that prevents that loop: a supplied parameter is
never named as missing, and when nothing is missing the hint says so instead of
listing requirements that are already met.
"""
import pytest
from pydantic import BaseModel, Field

from tools.controller.registry.service import build_validation_hint


class _TimelineParams(BaseModel):
    user: str
    max_results: int = Field(default=10, ge=5, le=100)


class _NoRequired(BaseModel):
    limit: int = Field(default=10, ge=1)


def test_supplied_param_is_never_reported_as_missing():
    hint = build_validation_hint(_TimelineParams, {"max_results": 3, "user": "tmachinroBot"})
    # `user` may well be NAMED (as one of the requirements that were met); what it
    # must never be is framed as absent, which is what sent the agent in a circle.
    assert "Missing required" not in hint
    assert "All required params were supplied" in hint
    assert "VALUE constraint" in hint


def test_the_hint_points_at_the_value_not_the_field_set():
    hint = build_validation_hint(_TimelineParams, {"max_results": 3, "user": "x"})
    assert "change the value, not the field set" in hint


def test_a_genuinely_missing_param_is_named():
    hint = build_validation_hint(_TimelineParams, {"max_results": 10})
    assert "Missing required: ['user']" in hint
    assert "VALUE constraint" not in hint


def test_all_params_are_still_listed_for_discovery():
    hint = build_validation_hint(_TimelineParams, {"user": "x", "max_results": 3})
    assert "All params:" in hint
    assert "max_results" in hint


def test_model_with_no_required_params_says_so():
    hint = build_validation_hint(_NoRequired, {"limit": 0})
    assert "no required params" in hint
    assert "Missing required" not in hint


def test_non_dict_args_do_not_invent_missing_fields():
    """A string/None args payload means 'we could not tell what was supplied'."""
    hint = build_validation_hint(_TimelineParams, "not-a-dict")
    # Nothing is known to be supplied, so both required names are honestly missing.
    assert "Missing required:" in hint
    assert "user" in hint


def test_unreadable_schema_yields_no_hint_rather_than_a_wrong_one():
    class _Exploding:
        @staticmethod
        def model_json_schema():
            raise RuntimeError("no schema")

    assert build_validation_hint(_Exploding, {"a": 1}) == ""


def test_hint_is_appended_by_the_converter(monkeypatch):
    """End to end through tool_calls_to_actions: the error text the agent reads."""
    from tools.controller.registry.service import Registry

    reg = Registry()

    class _Info:
        param_model = _TimelineParams

    reg.registry.actions["twitter_get_timeline"] = _Info()

    def _boom(name, args):
        raise ValueError("max_results: Input should be greater than or equal to 5")

    monkeypatch.setattr(reg, "tool_call_to_action", _boom)

    reg.tool_calls_to_actions([
        {"id": "call-1", "name": "twitter_get_timeline",
         "args": {"max_results": 3, "user": "tmachinroBot"}},
    ])
    msg = reg._last_validation_errors.get("call-1", "")
    assert "greater than or equal to 5" in msg
    assert "Required: ['user']" not in msg      # the misleading 2026-09-01 shape
    assert "Missing required" not in msg
