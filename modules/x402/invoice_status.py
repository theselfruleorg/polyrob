"""The ONE invoice-status vocabulary (interface audit D11, 2026-09-21).

Every owner seat's ``/invoices [status]`` filter and the ledger's income sum
import these — a ``refund_due`` (money taken, nothing delivered) or a
``settling`` (claimed, facilitator in flight) row is a real state the owner
must be able to ask for; the old three-word filter hid both. Lives beside
``invoicing.py`` rather than in it only because that module sits at its
size ceiling.
"""

INVOICE_STATUSES = ("pending", "settling", "completed", "settled_no_tx",
                    "expired", "refund_due")
#: The statuses that count as INCOME (the ledger never sums a refund_due row).
INCOME_STATUSES = ("completed", "settled_no_tx")
