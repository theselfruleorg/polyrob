"""Back-compat shim (067 P1b): the spend ledger lives in :mod:`core.money.ledger`.

``PolicyGate`` is the historical name of :class:`core.money.ledger.SpendLedger`;
every importer and ``isinstance`` check keeps working. New code imports
``SpendLedger`` from ``core.money.ledger``.
"""
from core.money.ledger import (  # noqa: F401
    _DAY_SECONDS,
    PolicyDecision,
    SpendLedger,
    _nonnegative_finite,
)

PolicyGate = SpendLedger
