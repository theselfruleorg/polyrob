"""An order that takes its goal back stops that goal.

The old goal can no longer spend, deliver or fail the order, but it ran its other
tools until it ended. ``modules`` cannot import the board, so the composition root
registers the canceller (``core.commerce.hooks``).
"""
import pytest

from core.commerce import hooks
from core.commerce import orders as ords
from core.commerce import store as st
from modules.commerce import service
from tests.unit.core.commerce.test_offer_store_orders import MANIFEST


@pytest.fixture
def stopped(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "db_path", lambda data_dir=None: str(tmp_path / "commerce.db"))
    seen = []
    hooks.register_goal_canceller(lambda goal_id: seen.append(goal_id) or True)
    yield seen
    hooks.register_goal_canceller(None)
    hooks.register_fulfiller(None)


def _fulfilling(max_attempts, goal_id="g_old", claimed_at=1000.0):
    d = dict(MANIFEST)
    d["fulfillment"] = {**MANIFEST["fulfillment"], "max_attempts": max_attempts}
    o = st.save_draft("owner", d)
    st.go_live(o.id, "owner", actor=st.OWNER_SEAT, expect_hash=st.manifest_hash(o.manifest))
    order = ords.create(offer_id=o.id, offer_version=1, user_id="owner", channel="http",
                        buyer="b", inputs={"delivery_address": "0x" + "ab" * 20, "brief": "x"},
                        amount_usd="5.00", spend_budget=0.5)
    ords.attach_invoice(order.id, "r1", "5.00")
    assert ords.mark_paid("r1", payment_tx="0x" + "11" * 32)
    token = ords.claim_fulfillment(order.id, now=claimed_at)
    if goal_id:
        assert ords.set_goal(order.id, token, goal_id)
    return ords.get(order.id)


@pytest.mark.asyncio
async def test_a_deadline_retry_stops_the_goal_it_left_behind(stopped):
    order = _fulfilling(max_attempts=3)
    out = await service.tick(now=1000.0 + 10 ** 7)
    assert out["retried"] == 1
    assert stopped == ["g_old"]
    assert ords.get(order.id).status == ords.PAID


@pytest.mark.asyncio
async def test_a_passed_deadline_with_no_attempts_left_stops_the_goal(stopped):
    order = _fulfilling(max_attempts=1)
    out = await service.tick(now=1000.0 + 10 ** 7)
    assert out["failed"] == 1
    assert stopped == ["g_old"]
    assert ords.get(order.id).status == ords.FAILED


@pytest.mark.asyncio
async def test_a_recorded_failure_stops_the_goal_after_the_notice(stopped, monkeypatch):
    """The caller can be the order's own run: a cancel before the notice would lose it."""
    order = _fulfilling(max_attempts=3)
    calls = []

    async def notice(container, user_id, text):
        calls.append(list(stopped))
    monkeypatch.setattr(service, "owner_notice", notice)

    out = await service.fail(order.id, "no", user_id="owner")
    assert out.status == ords.PAID
    assert calls == [[]]
    assert stopped == ["g_old"]


@pytest.mark.asyncio
async def test_a_refused_cancel_does_not_stop_the_retry(stopped):
    def boom(goal_id):
        raise RuntimeError("board locked")
    hooks.register_goal_canceller(boom)
    order = _fulfilling(max_attempts=3)
    out = await service.tick(now=1000.0 + 10 ** 7)
    assert out["retried"] == 1
    assert ords.get(order.id).status == ords.PAID


@pytest.mark.asyncio
async def test_without_a_canceller_the_retry_runs_and_says_so(stopped, monkeypatch):
    said = []  # not caplog: another test's logging config can disable this logger
    monkeypatch.setattr(service.logger, "warning", lambda msg, *a, **k: said.append(msg % a))
    hooks.register_goal_canceller(None)
    _fulfilling(max_attempts=3)
    out = await service.tick(now=1000.0 + 10 ** 7)
    assert out["retried"] == 1
    assert any("goal g_old is left running" in line for line in said)


def test_a_claim_token_is_no_goal(stopped):
    service._stop_goal("claim:abc")
    service._stop_goal(None)
    assert stopped == []
