"""Crash-persistent on-chain submission interlock.

An unresolved broadcast blocks fresh submissions until its cap charge is durably
booked. This is intentionally conservative: a timeout cannot free the interlock.
Stores public transaction identifiers only, never keys or signed payloads.
"""
import time
from pathlib import Path

#: Upper bound on a journaled replay key. Stored whole below this; refused above.
MAX_IDEMPOTENCY_KEY_CHARS = 16384


def journal_path(data_dir=None):
    from core.wallet.audit_sink import _wallet_data_dir
    return Path(_wallet_data_dir(data_dir, for_meta=True)) / 'submissions.sqlite'


def _identifier(value):
    # EVM hex is case-insensitive; Solana base58 signatures and holders are not.
    return value.lower() if value.startswith("0x") else value


def prepare(tx_hash, chain, holder, nonce, *, state='prepared', venue=None,
            idempotency_key=None):
    """Persist one submission BEFORE it is signed or sent.

    068 B5/B6: ``venue`` is the PolicyGate venue its spend books under, and
    ``idempotency_key`` the business replay key (the x402 fetch key). An
    operator release books under both, so a released row still counts against
    its venue cap and a retry of the same request is still refused as a replay.
    """
    from core.wallet.submission_store import connection
    if state not in ('prepared', 'reserved'):
        raise ValueError('invalid initial submission state')
    values = (_identifier(tx_hash), str(chain), _identifier(holder), str(nonce))
    if not values[0] or not values[1] or any(len(v) > limit for v, limit in zip(values, (256, 64, 256, 256))):
        raise ValueError('invalid submission identifier')
    venue = str(venue).strip().lower()[:64] if venue else None
    # 068 N2: the replay key is stored EXACTLY. It was cut to 512 characters, and
    # the x402 key carries the full URL, so a release booked a shortened key the
    # ledger's replay set never matched and a retry of the original request paid
    # again. An absurdly long key is refused (fail closed), never shortened.
    idem = str(idempotency_key) if idempotency_key else None
    if idem is not None and len(idem) > MAX_IDEMPOTENCY_KEY_CHARS:
        raise ValueError('replay key too long to journal exactly; refusing the submission')
    with connection(journal_path(), write=True) as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT tx_hash FROM submissions WHERE state != 'booked' LIMIT 1").fetchone()
        if row:
            raise ValueError(f'unaccounted wallet submission {row[0]}; reconcile before another send')
        db.execute('INSERT INTO submissions (tx_hash, chain, holder, nonce, created, state, '
                   'venue, idempotency_key) VALUES (?,?,?,?,?,?,?,?)',
                   (*values, time.time(), state, venue, idem))
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


def unresolved(data_dir=None):
    """The unresolved rows. 066 P1: a process that may not open the 0600 journal
    (the console runs as polyrob-web) reads the agent's public summary instead,
    which refuses when it is not bound to the journal as it is now."""
    from core.wallet.submission_store import connection, read_public_summary
    path = journal_path(data_dir)
    try:
        with connection(path) as db:
            if db is None:
                return []
            # The PUBLIC columns only — the same projection the console's summary
            # carries, so both readers agree. The 068 venue/idempotency_key
            # columns are read by the release, under the journal lock.
            from core.wallet.submission_store import _ROW_KEYS
            return [{k: row[k] for k in _ROW_KEYS} for row in
                    db.execute("SELECT * FROM submissions WHERE state != 'booked'")]
    except PermissionError:
        return read_public_summary(path)


def prepare_attempt(venue, holder, amount_usd, *, idempotency_key=None):
    """Interlock a payment/order before its external identifier is available.

    An ambiguous failure remains unresolved; only durable accounting or explicit
    operator reconciliation may release it. No signed payload or credentials.
    ``idempotency_key`` (068 B6) is the caller's replay key, kept so a release
    books under it.
    """
    import math
    import uuid
    amount = float(amount_usd)
    if not math.isfinite(amount) or amount < 0:
        raise ValueError("invalid submission amount")
    reference = "attempt:" + uuid.uuid4().hex
    prepare(reference, str(venue), str(holder or ""), str(amount),
            venue=str(venue), idempotency_key=idempotency_key)
    return reference


def operator_release(reference, book, *, data_dir=None):
    """068 B7: book and release ONE row as a single serialized operation.

    Holds the journal's write lock and an IMMEDIATE transaction across: re-read
    the row (still unresolved?) -> ``book(row)`` (the durable audit charge;
    itself idempotent per reference) -> mark it booked. A second release — in
    this process or another — waits on the lock, then finds the row booked and
    is refused; a crash after the charge and before the mark leaves the row
    unresolved, and the retry's ``book`` finds the existing charge instead of
    appending a second one. Returns whatever ``book`` returned.
    """
    from core.wallet.submission_store import connection
    ref = _identifier(str(reference or '').strip())
    if not ref:
        raise ValueError('no submission reference')
    with connection(journal_path(data_dir), write=True) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            row = db.execute("SELECT * FROM submissions WHERE tx_hash=? AND state != 'booked'",
                             (ref,)).fetchone()
            if row is None:
                raise ValueError(f'no unresolved submission {ref} (already released or booked)')
            result = book(dict(row))
            db.execute("UPDATE submissions SET state='booked' WHERE tx_hash=?", (ref,))
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return result


def operator_book(reference, *, data_dir=None):
    """068 X2: mark ONE unresolved row booked after the operator charged it.

    The only caller is ``core.wallet.submission_release.release_submission``,
    which writes the durable audit charge FIRST — a row is never released
    without its worst-case amount in the ledger the rolling caps read. Books a
    ``reserved`` (``signing:``) row too: that is exactly the row ``mark_booked``
    cannot resolve, and the one that otherwise blocks every send forever.
    """
    from core.wallet.submission_store import connection
    ref = _identifier(str(reference or '').strip())
    if not ref:
        raise ValueError('no submission reference')
    with connection(journal_path(data_dir), write=True) as db:
        with db:
            changed = db.execute(
                "UPDATE submissions SET state='booked' WHERE tx_hash=? AND state != 'booked'",
                (ref,)).rowcount
    if changed != 1:
        raise ValueError(f'no unresolved submission {ref}')
