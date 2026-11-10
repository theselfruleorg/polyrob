"""The owner reads "listed under /invoices refund_due" only when the invoice really moved
to refund_due; otherwise the notice says it still counts as income."""
import pytest

from core.commerce import orders as ords
from modules.commerce import service
from tests.unit.modules.commerce.test_order_service_e2e import _env  # noqa: F401  (autouse)


@pytest.fixture
def owner_told(monkeypatch):
    out = []

    async def _owner(container, user_id, text):
        out.append(text)

    async def _buyer(container, order, text, **k):
        return False
    monkeypatch.setattr(service, "owner_notice", _owner)
    monkeypatch.setattr(service, "tell_buyer", _buyer)
    return out


def _order(status_to=None):
    o = ords.create(offer_id="a", offer_version=1, user_id="owner", channel="a2a", buyer="b",
                    inputs={}, amount_usd="5", spend_budget=0)
    ords.attach_invoice(o.id, "r_1", "5")
    if status_to == ords.CANCELLED:
        ords.cancel(o.id)
    else:
        ords.mark_paid("r_1")
    return ords.get(o.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("marked", [True, False])
async def test_a_failed_order_notice_tells_the_truth_about_refund_due(monkeypatch, owner_told,
                                                                      marked):
    async def _mark(order, *, db=None):
        return marked
    monkeypatch.setattr(service, "mark_refund_due", _mark)
    await service._failed_notices(None, _order(), "deadline")
    (text,) = owner_told
    if marked:
        assert "now listed under /invoices refund_due" in text
    else:
        assert "listed under /invoices refund_due" not in text
        assert "could not be marked refund_due" in text.lower() and "income" in text


@pytest.mark.asyncio
async def test_a_payment_for_a_cancelled_order_names_an_unmarked_invoice(monkeypatch,
                                                                         owner_told):
    async def _mark(order, *, db=None):
        return False
    monkeypatch.setattr(service, "mark_refund_due", _mark)
    o = _order(ords.CANCELLED)
    await service.on_settled({"request_id": o.request_id, "user_id": "owner"})
    (text,) = owner_told
    assert "listed under /invoices refund_due" not in text
    assert "could not be marked refund_due" in text.lower()
