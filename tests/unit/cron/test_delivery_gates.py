"""033 T0.2 — cron delivery must use the GATED actions, not the raw helpers.

`cron/delivery.py` built a raw TwitterTool and called `post()`, which skips
_check_ready, the hourly rate limit, TWITTER_REQUIRE_APPROVAL, the cross-session
repeat-post cooldown and the social_write record. A cron job with
deliver=twitter published to the timeline with zero governance. `_deliver_email`
skipped `email_send`'s whole tier/allowlist/cap/seed stack the same way.
"""
import types

import pytest


class _Job:
    id = "job-1"
    user_id = "tenant-1"
    session_id = "sess-1"
    task = "a scheduled task"


@pytest.mark.asyncio
async def test_twitter_delivery_goes_through_the_gated_action(monkeypatch):
    import cron.delivery as d

    called = {}

    class _Tool:
        async def twitter_post(self, params, execution_context=None):
            called["text"] = params.text
            called["ctx"] = execution_context
            return types.SimpleNamespace(error=None, extracted_content="ok")

        async def post(self, *a, **k):
            raise AssertionError("cron must not call the ungated post() helper")

    monkeypatch.setattr(d, "_config_and_container", lambda ta: ({}, None))
    monkeypatch.setattr(d, "_build_twitter_tool", lambda cfg, c: _Tool())

    assert await d._deliver_twitter(None, _Job(), "hello world") is True
    assert called["text"] == "hello world"
    assert called["ctx"].user_id == "tenant-1"
    assert called["ctx"].session_id == "sess-1"


@pytest.mark.asyncio
async def test_twitter_delivery_reports_a_refusal_as_failure(monkeypatch):
    import cron.delivery as d

    class _Tool:
        async def twitter_post(self, params, execution_context=None):
            return types.SimpleNamespace(error="paused (social)", extracted_content=None)

    monkeypatch.setattr(d, "_config_and_container", lambda ta: ({}, None))
    monkeypatch.setattr(d, "_build_twitter_tool", lambda cfg, c: _Tool())
    assert await d._deliver_twitter(None, _Job(), "hi") is False


@pytest.mark.asyncio
async def test_delivery_context_is_autonomous_not_an_owner_turn():
    """role='leaf' is what makes _is_forged_or_autonomous_turn answer True."""
    import cron.delivery as d
    from tools.controller.turn_origin import _is_forged_or_autonomous_turn

    ctx = d._delivery_context(_Job())
    assert ctx.role == "leaf"
    assert _is_forged_or_autonomous_turn(ctx, object()) is True


@pytest.mark.asyncio
async def test_email_delivery_goes_through_the_gated_action(monkeypatch):
    import cron.delivery as d

    called = {}

    class _Tool:
        async def ensure_initialized(self):
            return None

        async def email_send(self, params, execution_context=None):
            called["to"] = params.to
            called["subject"] = params.subject
            called["ctx"] = execution_context
            return types.SimpleNamespace(error=None, extracted_content="ok")

        async def send_email(self, *a, **k):
            raise AssertionError("cron must not call the ungated send_email() helper")

    monkeypatch.setattr(d, "_config_and_container", lambda ta: ({}, None))
    monkeypatch.setattr(d, "_build_email_tool", lambda cfg, c: _Tool())
    monkeypatch.setattr(d, "_owner_email", lambda ta, job: "owner@example.com")

    assert await d._deliver_email(None, _Job(), "body", None) is True
    assert called["to"] == "owner@example.com"
    assert called["ctx"].user_id == "tenant-1"


@pytest.mark.asyncio
async def test_email_delivery_reports_a_tier_denial_as_failure(monkeypatch):
    import cron.delivery as d

    class _Tool:
        async def ensure_initialized(self):
            return None

        async def email_send(self, params, execution_context=None):
            return types.SimpleNamespace(error="denied: not owner or allowlisted",
                                         extracted_content=None)

    monkeypatch.setattr(d, "_config_and_container", lambda ta: ({}, None))
    monkeypatch.setattr(d, "_build_email_tool", lambda cfg, c: _Tool())
    monkeypatch.setattr(d, "_owner_email", lambda ta, job: "stranger@example.com")
    assert await d._deliver_email(None, _Job(), "body", None) is False
