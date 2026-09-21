"""A settled payment the agent owes BACK reaches a seat.

`modules/x402/x402_integration.py::mark_payment_refund_due` flips a SETTLED
machine payment to `refund_due` when the work failed downstream. Until
2026-09-21 that status existed only in the database: every invoice listing
filtered on `pending|completed|expired`, the ledger did not carry it, and the
delivery rail had no lane for it — so money the agent had taken and owed back
was invisible on every seat it has.

These pin the three halves that live in this partition: the rail LANE, the
status figure, and the Telegram filter.
"""
import pytest

from core.status_snapshot import Section, _money_section
from core.surfaces.user_delivery import (
    _CRITICAL_SOURCES, notice_subject, resolve_priority, PRIORITY_CRITICAL,
)


def _ledger(**treasury):
    base = {"income_usd": 10.0, "spend_usd": 1.0, "pending_usd": 0.0,
            "pending_count": 0, "refund_due_usd": 0.0, "refund_due_count": 0,
            "balance_usd": None, "net_usd": 9.0, "available": True}
    base.update(treasury)
    return {"runtime": {"spend_window_usd": 0.0, "spend_total_usd": 0.0,
                        "available": True},
            "treasury": base}


# --- the rail lane ----------------------------------------------------------

def test_a_refund_obligation_rides_the_uncappable_lane():
    """It is money the agent TOOK and owes a stranger back, and the obligation
    only grows while nobody acts on it. A daily cap may not drop it."""
    assert "payment_refund_due" in _CRITICAL_SOURCES
    assert resolve_priority("payment_refund_due", None) == PRIORITY_CRITICAL


def test_the_email_notice_says_what_it_is():
    """An owner notice on the mail rail must not arrive as `[POLYROB] <source>`
    — the subject is the only part he reads before deciding to open it."""
    subject = notice_subject("payment_refund_due")
    assert "refund" in subject.lower()
    assert "payment_refund_due" not in subject


# --- the status figure ------------------------------------------------------

def test_no_refund_owed_renders_no_line_and_no_health_item():
    sec = _money_section("u1", _ledger())
    assert not any("refund" in line.lower() for line in sec.lines)
    assert [h.key for h in sec.health] == []


def test_a_refund_owed_is_its_own_line_and_a_warning():
    sec = _money_section("u1", _ledger(refund_due_usd=12.5, refund_due_count=3))
    line = [l for l in sec.lines if "refund owed" in l]
    assert line, sec.lines
    assert "$12.50" in line[0] and "3 settled payment(s)" in line[0]
    item = [h for h in sec.health if h.key == "payment_refund_due"]
    assert item, [h.key for h in sec.health]
    assert "12.50" in item[0].text
    # The remedy names a VERB the owner can type, never an env flag.
    assert "/invoices" in item[0].remedy


def test_a_refund_owed_is_never_netted_into_the_treasury_figure():
    """⚠️ Netting a debt against income shows a HEALTHIER treasury the more the
    agent owes. The net line must be identical with and without the debt."""
    without = _money_section("u1", _ledger())
    with_debt = _money_section("u1", _ledger(refund_due_usd=99.0,
                                             refund_due_count=2))
    net_a = [l for l in without.lines if l.startswith("treasury cash flow")]
    net_b = [l for l in with_debt.lines if l.startswith("treasury cash flow")]
    assert net_a == net_b


# --- the Telegram filter ----------------------------------------------------

@pytest.mark.asyncio
async def test_invoices_accepts_every_status_the_store_can_hold(monkeypatch):
    """The filter vocabulary is the store's, imported — not a local triple.

    `settling` and `refund_due` are the two that were unaskable, and
    `refund_due` is the one that means the agent owes somebody money.
    """
    from modules.x402.invoicing import INVOICE_STATUSES
    from surfaces.telegram import owner_ops

    asked = []

    async def _list(*, user_id, status=None, limit=20, db=None):
        asked.append(status)
        return []

    monkeypatch.setattr("modules.x402.invoicing.list_payment_requests", _list)
    for status in INVOICE_STATUSES:
        out = await owner_ops.invoices_reply("u1", [status])
        assert "Usage:" not in out, f"{status} was refused as unknown"
    assert asked == list(INVOICE_STATUSES)


@pytest.mark.asyncio
async def test_an_unknown_status_names_every_real_one(monkeypatch):
    from modules.x402.invoicing import INVOICE_STATUSES
    from surfaces.telegram import owner_ops

    out = await owner_ops.invoices_reply("u1", ["nonsense"])
    assert out.startswith("Usage:")
    for status in INVOICE_STATUSES:
        assert status in out


@pytest.mark.asyncio
async def test_a_refund_row_in_the_listing_says_it_is_owed(monkeypatch):
    """A `refund_due` row under the same "Mark one paid" remedy as a pending
    receivable reads as income. It is the opposite."""
    from surfaces.telegram import owner_ops

    async def _list(*, user_id, status=None, limit=20, db=None):
        return [{"request_id": "r1", "status": "refund_due", "amount_usd": 4.0,
                 "purpose": "a scrape that failed"},
                {"request_id": "r2", "status": "pending", "amount_usd": 9.0,
                 "purpose": "a report"}]

    monkeypatch.setattr("modules.x402.invoicing.list_payment_requests", _list)
    out = await owner_ops.invoices_reply("u1", [])
    assert "REFUNDS I owe" in out
    assert "$4.00" in out
    assert "/invoices refund_due" in out
