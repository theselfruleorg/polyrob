"""033 T0.2 + 067 P3b — the X pack's ``twitter`` cron delivery channel.

Cron delivery used to build a raw TwitterTool and call ``post()``, which skips
_check_ready, the hourly rate limit, TWITTER_REQUIRE_APPROVAL, the cross-session
repeat-post cooldown and the social_write record. The channel (moved out of
``cron/delivery.py`` into ``polyrob_x.cron_delivery``) must go through the gated
ACTION and record the external write. Without the pack, ``deliver=twitter`` is
reported unavailable with the reason (tests/unit/core/packs/test_pack_chat_surfaces.py).
"""
import types

import pytest

pytest.importorskip("polyrob_x")


class _Job:
    id = "job-1"
    user_id = "tenant-1"
    session_id = "sess-1"
    task = "a scheduled task"


def test_the_pack_registers_the_twitter_channel():
    from core.delivery_channels import sender_for
    from cron.delivery import allowed_targets
    import polyrob_x.cron_delivery as d
    assert "twitter" in allowed_targets()
    assert sender_for("twitter") is d.deliver_post


@pytest.mark.asyncio
async def test_twitter_delivery_goes_through_the_gated_action(monkeypatch):
    import polyrob_x.cron_delivery as d

    called = {}

    class _Tool:
        async def twitter_post(self, params, execution_context=None):
            called["text"] = params.text
            called["ctx"] = execution_context
            return types.SimpleNamespace(error=None, extracted_content="ok")

        async def post(self, *a, **k):
            raise AssertionError("cron must not call the ungated post() helper")

    monkeypatch.setattr("cron.delivery._config_and_container", lambda ta: ({}, None))
    monkeypatch.setattr(d, "_build_twitter_tool", lambda cfg, c: _Tool())

    assert await d.deliver_post(None, _Job(), "hello world") is True
    assert called["text"] == "hello world"
    assert called["ctx"].user_id == "tenant-1"
    assert called["ctx"].session_id == "sess-1"


@pytest.mark.asyncio
async def test_twitter_delivery_reports_a_refusal_as_failure(monkeypatch):
    import polyrob_x.cron_delivery as d

    class _Tool:
        async def twitter_post(self, params, execution_context=None):
            return types.SimpleNamespace(error="paused (social)", extracted_content=None)

    monkeypatch.setattr("cron.delivery._config_and_container", lambda ta: ({}, None))
    monkeypatch.setattr(d, "_build_twitter_tool", lambda cfg, c: _Tool())
    assert await d.deliver_post(None, _Job(), "hi") is False


@pytest.mark.asyncio
async def test_cron_delivery_records_the_external_write(monkeypatch, tmp_path):
    """033: cron calls the action outside any Controller, so the effect hook
    never sees it — cron/delivery.py is its own seam, success and refusal."""
    import core.event_log as el
    import polyrob_x.cron_delivery as d
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "true")
    el._INSTANCES.clear()

    results = iter([None, "paused (social)"])

    class _Tool:
        async def twitter_post(self, params, execution_context=None):
            return types.SimpleNamespace(error=next(results), extracted_content="ok")

    monkeypatch.setattr("cron.delivery._config_and_container", lambda ta: ({}, None))
    monkeypatch.setattr(d, "_build_twitter_tool", lambda cfg, c: _Tool())
    assert await d.deliver_post(None, _Job(), "hello world") is True
    assert await d.deliver_post(None, _Job(), "hello again") is False
    rows = el.get_event_log().query(kind="external_write")
    el._INSTANCES.clear()
    assert [r["attrs"]["outcome"] for r in rows] == ["error", "ok"]
    assert all(r["effect"] == "social" and r["attrs"]["surface"] == "cron"
               and r["attrs"]["autonomous"] is True and r["user_id"] == "tenant-1"
               for r in rows)
