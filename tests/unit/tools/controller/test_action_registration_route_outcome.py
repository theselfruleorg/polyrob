"""send_message's honest delivery-outcome reporting (2026-08-28 fix).

Root cause of the live incident: send_message's ActionResult always said
"Message sent to user (non-blocking)" regardless of what the delivery rail
actually did, so a capped/failed/dropped send was indistinguishable from a
real one in the session transcript — which is exactly what let an owner
reply go undelivered while everything LOOKED successful.
"""
from tools.controller.action_registration import _describe_route_outcome


def test_none_outcome_is_unchanged():
    assert _describe_route_outcome(None) is None


def test_sent_outcome_is_unchanged():
    assert _describe_route_outcome("sent") is None


def test_capped_is_reported_as_not_delivered():
    out = _describe_route_outcome("capped")
    assert out is not None
    assert "NOT delivered" in out
    assert "daily message cap" in out


def test_rate_limited_is_reported_as_not_delivered():
    out = _describe_route_outcome("rate_limited")
    assert "NOT delivered" in out


def test_deduped_is_reported_as_not_delivered():
    out = _describe_route_outcome("deduped")
    assert "NOT delivered" in out


def test_failed_is_reported_as_not_delivered():
    out = _describe_route_outcome("failed")
    assert "NOT delivered" in out


def test_fallback_is_reported_as_durable_not_instant():
    out = _describe_route_outcome("fallback")
    assert out is not None
    assert "NOT delivered" not in out
    assert "durable" in out


def test_unknown_outcome_falls_back_to_generic_message():
    out = _describe_route_outcome("some_new_outcome")
    assert "NOT delivered" in out
    assert "some_new_outcome" in out
