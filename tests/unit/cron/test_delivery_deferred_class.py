"""D44/D45 — a deferred cron delivery is not a failed one, and the five router
surfaces ride the ONE rail."""
import pytest

from cron import delivery


class _Job:
    id = "job-1"
    user_id = "u1"
    task = "the nightly report"


def test_a_held_or_deduped_rail_outcome_is_deferred_not_failed():
    for outcome in delivery.DEFERRED_RAIL_OUTCOMES:
        assert delivery.classify_rail_outcome(outcome) == "deferred"
    assert delivery.classify_rail_outcome("sent") == "sent"
    assert delivery.classify_rail_outcome("no_sink") == "failed"
    assert delivery.classify_rail_outcome(None) == "failed"


def test_delivery_outcome_passes_a_rail_class_through():
    assert delivery.delivery_outcome("the digest", "deferred") == "deferred"
    assert delivery.delivery_outcome("[SILENT] nothing", "deferred") == "suppressed"
    # legacy bool contract unchanged
    assert delivery.delivery_outcome("x", True) == "sent"
    assert delivery.delivery_outcome("x", False) == "failed"


@pytest.mark.asyncio
async def test_a_router_surface_goes_through_the_user_delivery_rail(monkeypatch):
    """D44: slack/discord/signal/whatsapp/x reports used to call the router shim
    directly — no dedup, no cap, and no durable notice when the send failed."""
    seen = {}

    async def _rail(container, user_id, text, **kw):
        seen.update({"user_id": user_id, "text": text, **kw})
        return "sent"

    import core.surfaces.user_delivery as ud
    monkeypatch.setattr(ud, "deliver_user_message", _rail)
    monkeypatch.setattr("core.surfaces.owner_address.owner_address",
                        lambda c, s, u: "U123")

    class _Agent:
        config = None
        container = None

    out = await delivery._deliver_router_surface(_Agent(), _Job(), "all healthy",
                                                 "slack", None)
    assert out == "sent"
    assert seen["recipient_surface"] == "slack"
    assert seen["recipient_override"] == "U123"
    assert seen["source"] == "cron"


@pytest.mark.asyncio
async def test_a_deferred_router_delivery_is_not_reported_as_failed(monkeypatch):
    async def _rail(container, user_id, text, **kw):
        return "quiet_held"

    import core.surfaces.user_delivery as ud
    monkeypatch.setattr(ud, "deliver_user_message", _rail)
    monkeypatch.setattr("core.surfaces.owner_address.owner_address",
                        lambda c, s, u: "U123")

    class _Agent:
        config = None
        container = None

    assert await delivery._deliver_router_surface(
        _Agent(), _Job(), "report", "discord", None) == "deferred"


@pytest.mark.asyncio
async def test_a_queued_cross_process_delivery_is_deferred_not_failed(monkeypatch):
    """⚠️ Revalidation of D44/D45 together.

    None of the five router surfaces is hosted in the agent process, so EVERY
    one of their reports is handed to the durable cross-process queue. The rail
    answered that with ``fallback``, which classified as ``failed`` — so the
    fix that stopped a working rail reading as a broken one journalled the
    D44 surfaces as broken on every single run. ``queued`` is now its own
    answer; ``fallback`` (a live sink that REFUSED) stays a fault.
    """
    async def _rail(container, user_id, text, **kw):
        return "queued"

    import core.surfaces.user_delivery as ud
    monkeypatch.setattr(ud, "deliver_user_message", _rail)
    monkeypatch.setattr("core.surfaces.owner_address.owner_address",
                        lambda c, s, u: "U123")

    class _Agent:
        config = None
        container = None

    assert await delivery._deliver_router_surface(
        _Agent(), _Job(), "report", "slack", None) == "deferred"
    assert delivery.classify_rail_outcome("queued") == "deferred"
    assert delivery.classify_rail_outcome("fallback") == "failed"


@pytest.mark.asyncio
async def test_the_rail_reports_queued_when_the_durable_queue_took_the_body(
        monkeypatch):
    """The producer side of the same fact, at the rail itself."""
    from core.surfaces import user_delivery as ud

    class _Router:
        async def send_message_ex(self, _addr, _text, **_kw):
            return "queued"

    class _Container:
        def get_service(self, name):
            return _Router() if name == "message_router" else None

        config = None

    monkeypatch.setattr(ud, "resolve_telegram_recipient", lambda c, u: "777")
    monkeypatch.setattr("core.surfaces.owner_address.owner_surface_order",
                        lambda: ["telegram"])
    monkeypatch.setattr("core.surfaces.owner_address.owner_address",
                        lambda c, s, u: "777")
    out = await ud.deliver_user_message(_Container(), "u1", "the report",
                                        source="cron", event_log=None)
    assert out == "queued"


@pytest.mark.asyncio
async def test_a_silent_run_is_suppressed_not_failed():
    assert await delivery.deliver_result_ex(
        None, _Job(), "all good [SILENT]", target="email") == "suppressed"
