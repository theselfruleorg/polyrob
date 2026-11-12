"""Who PAID this instance: the payer addresses of its completed invoices (115 W5).

One store knows a payer address: ``x402_payment_requests`` in ``bot.db``. An order
(``core/commerce/orders.py``) keeps its money state on that invoice row, so an order
needs no second read. A row names its payer in the ``payer_address`` column (the
facilitator ``/pay`` path) or in ``metadata.payer_address`` (stamped by the on-chain
settlement scan, ``settlement_scan._stamp_settlement_source``).

Counted: a payable row (``invoicing.PAYABLE_KINDS``: an agent invoice, a room action, an
order) whose status is income (``INCOME_STATUSES``, i.e. ``completed``). A refunded or
refund-due row, a pending one, an unproven ``settled_no_tx`` one and a machine payment
are not "an accepted order".

⚠️ A payer address is often an exchange (``orders.py``: never trust it), so a match
says "this address sent a settled payment here", not "this person bought".

⚠️ An unreadable store is not an empty one. A store that exists and cannot be read —
or that has no invoice table — raises :class:`PaidPayersUnreadable`. Only a missing
file reads as "nobody paid" (no invoice was ever written), and a read never creates it.

Sync on purpose: the account view that shows it (``tools/agent_nft/view.py``) is a sync
build over a pinned RPC. It opens the file ``mode=ro`` per call.
"""
from __future__ import annotations

import os
import sqlite3
from typing import FrozenSet, Optional

from modules.x402.invoice_status import INCOME_STATUSES
from modules.x402.invoicing import PAYABLE_KINDS


class PaidPayersUnreadable(RuntimeError):
    """The invoice store exists and could not be read. Never read this as "none paid"."""


def _family(chain: str, address: str) -> Optional[str]:
    """The row's chain family, when the payer address has that family's shape."""
    from core.wallet.addresses import family_of
    evm_shape = (len(address) == 42 and address[:2].lower() == "0x"
                 and all(c in "0123456789abcdefABCDEF" for c in address[2:]))
    fam = family_of(chain)
    if fam is None and evm_shape:
        return "evm"   # a test chain outside the registry (base-sepolia): the EVM shape
    if (fam == "evm") != evm_shape:
        return None    # an address of the other family on this row: not this family's payer
    return fam


def paid_payer_addresses(*, family: str = "evm",
                         db_path: Optional[str] = None) -> FrozenSet[str]:
    """The payer addresses of this instance's completed invoices on *family* chains.

    EVM addresses come back lowercase; an svm address keeps its case (base58).
    *db_path* defaults to ``core.runtime_paths.bot_db_path()``, the one bot.db path.
    Raises :class:`PaidPayersUnreadable` when the store cannot be read."""
    if db_path is None:
        from core.runtime_paths import bot_db_path
        db_path = bot_db_path()
    if not os.path.exists(db_path):
        return frozenset()
    from core.sqlite_util import read_only_connect
    statuses = tuple(INCOME_STATUSES)
    sql = ("SELECT chain, COALESCE(NULLIF(payer_address, ''), "
           "json_extract(metadata, '$.payer_address')) AS payer "
           "FROM x402_payment_requests "
           f"WHERE status IN ({','.join('?' * len(statuses))}) "
           f"AND json_extract(metadata, '$.kind') IN ({','.join('?' * len(PAYABLE_KINDS))})")
    try:
        conn = read_only_connect(db_path)
        try:
            rows = conn.execute(sql, statuses + tuple(PAYABLE_KINDS)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise PaidPayersUnreadable(f"the invoice store {db_path} could not be read: {exc}") from exc
    out = set()
    for chain, payer in rows:
        address = str(payer or "").strip()
        if not address or _family(str(chain or ""), address) != family:
            continue
        out.add(address.lower() if family == "evm" else address)
    return frozenset(out)
