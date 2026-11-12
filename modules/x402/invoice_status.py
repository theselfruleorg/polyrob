"""The ONE invoice-status vocabulary (interface audit D11, 2026-09-21).

Every owner seat's ``/invoices [status]`` filter and the ledger's income sum
import these — a ``refund_due`` (money taken, nothing delivered) or a
``settling`` (claimed, facilitator in flight) row is a real state the owner
must be able to ask for; the old three-word filter hid both. Lives beside
``invoicing.py`` rather than in it only because that module sits at its
size ceiling.

098 S1: :data:`X402_INVOICE` is the transition table every status write of
``x402_payment_requests`` checks. ``settling`` is stored for two sub-states
that only the row's columns separate: a local CLAIM (``claim_lane`` set,
``metadata.facilitator_submitted`` 0 — always safe to release) and a
SUBMISSION (``facilitator_submitted`` 1 — released only on a definite answer).
"""
from core.state.machine import StateMachine

INVOICE_STATUSES = ("pending", "settling", "completed", "settled_no_tx",
                    "expired", "refund_due", "refunded")
#: The statuses that count as INCOME (the ledger never sums a refund_due row).
#: 098 MO-21: a ``settled_no_tx`` row has no on-chain proof — it is UNPROVEN
#: (never income) until a tx completes it.
INCOME_STATUSES = ("completed",)
UNPROVEN_STATUSES = ("settled_no_tx",)

X402_INVOICE = StateMachine(
    name="x402_invoice",
    states=frozenset(INVOICE_STATUSES),
    transitions={
        # pending -> settling: `/pay` claim only; -> completed: a scan pass,
        # the owner or the resolver settles in one CAS; -> expired: expiry.
        "pending": frozenset({"settling", "completed", "expired"}),
        # settling -> pending: an unsubmitted claim is released (revert, the
        # claim reaper, an owner reopen) or a submission is DEFINITELY refused.
        # -> expired (098 review O-03): the owner checked the chain after the
        # payer's authorization deadline and no payment exists.
        "settling": frozenset({"pending", "completed", "refund_due", "expired"}),
        "completed": frozenset({"refund_due"}),
        "settled_no_tx": frozenset({"completed", "refund_due", "expired"}),
        "refund_due": frozenset({"refunded"}),
    },
    terminal=frozenset({"expired", "refunded"}),
    # machine payments are INSERTed settling/completed/settled_no_tx.
    initial=frozenset({"pending", "settling", "completed", "settled_no_tx"}),
)


def sql_now() -> str:
    """``datetime('now')`` as a bound value (UTC, SQLite's own format), for a
    guarded write whose SET columns are parameters."""
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
