"""An order invoice reaches ``on_settled`` only as ``completed``, so the service has no
branch for anything else. This pins the fact the removal stands on."""
from modules.x402.invoice_status import X402_INVOICE


def test_no_transition_enters_settled_no_tx():
    assert all("settled_no_tx" not in targets for targets in X402_INVOICE.transitions.values())

