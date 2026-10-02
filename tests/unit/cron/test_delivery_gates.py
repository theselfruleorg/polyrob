"""033 T0.2 — cron delivery must use the GATED actions, not the raw helpers.

`_deliver_email` skipped `email_send`'s whole tier/allowlist/cap/seed stack.
The twitter half (the X pack's ``twitter`` delivery channel, 067 P3b) is in
tests/packs/x/test_cron_delivery_channel.py.
"""
import types

import pytest


class _Job:
    id = "job-1"
    user_id = "tenant-1"
    session_id = "sess-1"
    task = "a scheduled task"


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


@pytest.mark.asyncio
async def test_room_delivery_respects_a_pause_set_while_cron_ran(tmp_path, monkeypatch):
    import cron.delivery as d
    from core.autonomy_control import pause
    monkeypatch.setenv("EXTERNAL_WRITE_PAUSE_GATE", "true")
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path))
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "false")
    pause(str(tmp_path), scopes=("all",))
    class Router:
        async def send_message(self, *args):
            pytest.fail("an autonomous room post escaped the pause")
    container = types.SimpleNamespace(
        config=types.SimpleNamespace(data_dir=str(tmp_path)),
        get_service=lambda key: Router() if key == "message_router" else None)
    assert await d._deliver_room(container, _Job(), "report", "-100") == "deferred"


@pytest.mark.asyncio
async def test_room_delivery_pause_probe_fails_closed(monkeypatch):
    import cron.delivery as d
    import core.effects as effects
    monkeypatch.setenv("EXTERNAL_WRITE_PAUSE_GATE", "true")
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "false")
    def unavailable(*a, **k):
        raise OSError("unreadable")
    monkeypatch.setattr(effects, "effect_pause_refusal", unavailable)
    assert await d._deliver_room(None, _Job(), "report", "-100") == "deferred"
