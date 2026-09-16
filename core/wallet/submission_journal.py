"""Crash-persistent on-chain submission interlock.

An unresolved broadcast blocks fresh submissions until its cap charge is durably
booked. This is intentionally conservative: a timeout cannot free the interlock.
Stores public transaction identifiers only, never keys or signed payloads.
"""
import time
from pathlib import Path


def journal_path():
    from core.wallet.audit_sink import _wallet_data_dir
    return Path(_wallet_data_dir(for_meta=True)) / 'submissions.sqlite'


def _identifier(value):
    # EVM hex is case-insensitive; Solana base58 signatures and holders are not.
    return value.lower() if value.startswith("0x") else value


def prepare(tx_hash, chain, holder, nonce, *, state='prepared'):
    from core.wallet.submission_store import connection
    if state not in ('prepared', 'reserved'):
        raise ValueError('invalid initial submission state')
    values = (_identifier(tx_hash), str(chain), _identifier(holder), str(nonce))
    if not values[0] or not values[1] or any(len(v) > limit for v, limit in zip(values, (256, 64, 256, 256))):
        raise ValueError('invalid submission identifier')
    with connection(journal_path(), write=True) as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT tx_hash FROM submissions WHERE state != 'booked' LIMIT 1").fetchone()
        if row:
            raise ValueError(f'unaccounted wallet submission {row[0]}; reconcile before another send')
        db.execute('INSERT INTO submissions VALUES (?,?,?,?,?,?)',
                   (*values, time.time(), state))
        db.commit()  # Must complete BEFORE any network submission.


def reserve_signing(chain, holder, nonce):
    """Persist uncertainty BEFORE invoking signing code, even if it raises."""
    import uuid
    reference = 'signing:' + uuid.uuid4().hex
    prepare(reference, chain, holder, nonce, state='reserved')
    return reference


def bind_signed_hash(reference, tx_hash):
    """Bind one reserved signing attempt to its locally derived public hash."""
    from core.wallet.submission_store import connection
    tx_hash = _identifier(tx_hash)
    if not tx_hash or len(tx_hash) > 256 or tx_hash.startswith(('signing:', 'attempt:')):
        raise ValueError('invalid signed transaction identifier')
    with connection(journal_path(), write=True) as db:
        with db:
            changed = db.execute("UPDATE submissions SET tx_hash=?, state='prepared' WHERE tx_hash=? AND state='reserved'",
                                 (tx_hash, reference)).rowcount
            if changed != 1:
                raise ValueError('signing reservation missing or already bound')


def mark_booked(tx_hash, *, amount_usd=None, venue=None):
    if not tx_hash:
        return
    from core.wallet.submission_store import connection
    with connection(journal_path()) as check:
        if check is None:
            return
    with connection(journal_path(), write=True) as db:
        with db:
            row = db.execute('SELECT * FROM submissions WHERE tx_hash=?', (_identifier(tx_hash),)).fetchone()
            if row is None or row['state'] == 'booked':
                return
            if row['state'] == 'reserved':
                raise ValueError('unresolved signing attempt cannot be booked without its transaction identifier')
            if row['tx_hash'].startswith('attempt:'):
                import math
                amount = float(amount_usd) if amount_usd is not None else -1
                expected = float(row['nonce'])
                if (not math.isfinite(amount) or not math.isfinite(expected) or expected < 0
                        or amount < expected or str(venue).lower() != row['chain'].lower()):
                    raise ValueError('durable charge does not cover the prepared venue attempt')
            db.execute("UPDATE submissions SET state='booked' WHERE tx_hash=?", (_identifier(tx_hash),))


def unresolved():
    from core.wallet.submission_store import connection
    with connection(journal_path()) as db:
        if db is None:
            return []
        return [dict(row) for row in db.execute("SELECT * FROM submissions WHERE state != 'booked'")]


def prepare_attempt(venue, holder, amount_usd):
    """Interlock a payment/order before its external identifier is available.

    An ambiguous failure remains unresolved; only durable accounting or explicit
    operator reconciliation may release it. No signed payload or credentials.
    """
    import math
    import uuid
    amount = float(amount_usd)
    if not math.isfinite(amount) or amount < 0:
        raise ValueError("invalid submission amount")
    reference = "attempt:" + uuid.uuid4().hex
    prepare(reference, str(venue), str(holder or ""), str(amount))
    return reference
