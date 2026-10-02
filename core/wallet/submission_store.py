"""Single-host journal durability checks, shared by readers and writers.

A sidecar binds the SQLite database identity and minimum row count. Missing,
replaced or truncated history refuses spending. This detects storage loss, not
an attacker with authority to rewrite every custody file.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time
import uuid


class JournalUnavailable(ValueError):
    pass


def _regular(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise JournalUnavailable('submission journal files must be regular, without link aliases')
    return True


def _marker(path):
    if not _regular(path):
        return None
    with path.open('rb') as stream:
        raw = stream.read(1025)
    if len(raw) > 1024:
        raise JournalUnavailable('submission journal marker exceeds budget')
    try:
        value = json.loads(raw)
        if (set(value) != {'identity', 'rows'} or not isinstance(value['identity'], str)
                or not re.fullmatch(r'[0-9a-f]{32}', value['identity']) or type(value['rows']) is not int
                or value['rows'] < 0):
            raise ValueError('invalid marker')
        return value
    except (ValueError, TypeError, KeyError) as exc:
        raise JournalUnavailable('invalid submission journal marker') from exc


def _write_marker(path, identity, rows):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent,
                                         prefix='.submissions-', delete=False) as stream:
            temporary = stream.name
            json.dump({'identity': identity, 'rows': rows}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _validate(db, marker):
    # Unknown states must block rather than being silently excluded by a query.
    invalid = db.execute("""SELECT 1 FROM submissions WHERE
        typeof(tx_hash) != 'text' OR length(tx_hash) NOT BETWEEN 1 AND 256 OR
        typeof(chain) != 'text' OR length(chain) NOT BETWEEN 1 AND 64 OR
        typeof(holder) != 'text' OR length(holder) > 256 OR
        typeof(nonce) != 'text' OR length(nonce) > 256 OR
        typeof(created) NOT IN ('integer', 'real') OR created < 0 OR created > 1e100 OR
        state NOT IN ('reserved', 'prepared', 'booked') OR state IS NULL LIMIT 1""").fetchone()
    if invalid:
        raise JournalUnavailable('invalid submission journal history')
    count = db.execute('SELECT count(*) FROM submissions').fetchone()[0]
    has_meta = db.execute("SELECT 1 FROM sqlite_master WHERE name='submission_identity' AND type='table'").fetchone()
    identity = None
    if has_meta:
        identities = db.execute('SELECT identity FROM submission_identity LIMIT 2').fetchall()
        if (len(identities) != 1 or not isinstance(identities[0][0], str)
                or not re.fullmatch(r'[0-9a-f]{32}', identities[0][0])):
            raise JournalUnavailable('invalid submission journal identity')
        identity = identities[0][0]
    if marker is not None:
        if identity != marker['identity'] or count < marker['rows']:
            raise JournalUnavailable('submission journal replaced or truncated; reconcile storage')
    elif identity is not None:
        # A migrated journal never downgrades back into the legacy path.
        raise JournalUnavailable('submission journal marker missing; reconcile storage')
    return identity, count


#: 068 B5/B6: columns added after the first release. NULL on a legacy row —
#: ``core.wallet.submission_release.booking_venue`` then derives the venue.
_ADDED_COLUMNS = (("venue", "TEXT"), ("idempotency_key", "TEXT"))


def _migrate_columns(db):
    """Add the 068 columns to an intact legacy table (writers only; additive)."""
    have = {row[1] for row in db.execute("PRAGMA table_info(submissions)")}
    missing = [(name, kind) for name, kind in _ADDED_COLUMNS if name not in have]
    if missing:
        with db:
            for name, kind in missing:
                db.execute(f"ALTER TABLE submissions ADD COLUMN {name} {kind}")


@contextmanager
def connection(path: Path, *, write=False):
    """Yield a validated connection (None only for a genuinely unused journal).

    Writers migrate intact legacy databases under the same advisory lock.
    Reads never create a database or repair its metadata. All processes must use
    this implementation; a legacy writer does not honor the shared file lock.
    """
    path = path.absolute()
    marker_path = Path(str(path) + '.hwm')
    exists = _regular(path)
    marker = _marker(marker_path)
    if not exists and not write:
        if marker is not None:
            raise JournalUnavailable('submission journal missing; reconcile storage')
        yield None
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    import fcntl
    lock_path = Path(str(path) + '.lock')
    _regular(lock_path)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    db = None
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise JournalUnavailable('submission journal lock timed out')
                time.sleep(.01)
        # Recheck after locking, including creation by a competing process.
        exists, marker = _regular(path), _marker(marker_path)
        if not exists and marker is not None:
            raise JournalUnavailable('submission journal missing; reconcile storage')
        if not exists:
            if not write:
                yield None
                return
            # Exclusive creation means a zero-length file left by a crash is
            # refused on the next run instead of becoming fresh history.
            created = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(created)
        elif path.stat().st_size == 0:
            raise JournalUnavailable('empty submission journal; reconcile storage')
        db = sqlite3.connect(path.as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        if write:
            db.execute('PRAGMA synchronous=FULL')
        if not exists:
            with db:
                db.execute('''CREATE TABLE submissions (
                    tx_hash TEXT PRIMARY KEY, chain TEXT NOT NULL, holder TEXT NOT NULL,
                    nonce TEXT NOT NULL, created REAL NOT NULL, state TEXT NOT NULL,
                    venue TEXT, idempotency_key TEXT)''')
        identity, count = _validate(db, marker)
        if write:
            _migrate_columns(db)
        if write and identity is None:
            identity = uuid.uuid4().hex
            with db:
                db.execute('CREATE TABLE submission_identity (identity TEXT NOT NULL)')
                db.execute('INSERT INTO submission_identity VALUES (?)', (identity,))
            _write_marker(marker_path, identity, count)
        yield db
        if write:
            # Callers commit before returning. Persist the high-water count
            # before allowing a signing/network operation to follow the call.
            _, count = _validate(db, _marker(marker_path))
            _write_marker(marker_path, identity, count)
            _write_public_summary(db, path, identity, count)
    finally:
        if db is not None:
            db.close()
        os.close(fd)


# --- the public summary (066 P1: the console reads this, not the journal) ---- #
#
# 066 P0.4 pinned the journal and its marker 0600 to the agent, so the console
# (polyrob-web) can no longer open them — and must not: it would need write
# access to take the flock. After EVERY write the agent also writes
# ``submissions.public.json`` (0640, group-readable): the unresolved rows —
# public identifiers only, the same columns ``unresolved()`` returns — bound to
# the journal's identity and to the journal FILE's stat at that moment. A reader
# that cannot open the journal reads the summary, and refuses it when the
# journal changed after it was written (a crash between commit and summary): an
# unreadable journal is never reported as a clean one.

PUBLIC_SUMMARY = 'submissions.public.json'
_SUMMARY_KEYS = {'identity', 'rows', 'journal', 'unresolved', 'written_at'}
_ROW_KEYS = ('tx_hash', 'chain', 'holder', 'nonce', 'created', 'state')


def public_summary_path(path: Path) -> Path:
    return Path(path).absolute().parent / PUBLIC_SUMMARY


def _journal_stat(path: Path) -> dict:
    info = Path(path).stat()
    return {'ino': info.st_ino, 'size': info.st_size, 'mtime_ns': info.st_mtime_ns}


def _write_public_summary(db, path: Path, identity, count) -> None:
    """Fail-open: a summary that cannot be written leaves the console saying
    "unavailable" (it refuses a stale one), never the money path broken."""
    temporary = None
    try:
        rows = [{k: row[k] for k in _ROW_KEYS} for row in
                db.execute("SELECT * FROM submissions WHERE state != 'booked'")]
        value = {'identity': identity, 'rows': count, 'journal': _journal_stat(path),
                 'unresolved': rows, 'written_at': time.time()}
        target = public_summary_path(path)
        with tempfile.NamedTemporaryFile(mode='w', dir=target.parent,
                                         prefix='.submissions-public-', delete=False) as stream:
            temporary = stream.name
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, target)
        temporary = None
    except Exception as exc:  # noqa: BLE001 — never break a send over the mirror
        import logging
        logging.getLogger(__name__).warning('submission journal public summary not written: %s', exc)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def read_public_summary(path: Path) -> list:
    """The unresolved rows, from the summary, for a process that may not open
    the journal. Raises :class:`JournalUnavailable` unless the summary is bound
    to the journal file as it is NOW."""
    path = Path(path).absolute()
    target = public_summary_path(path)
    try:
        info = target.lstat()
    except FileNotFoundError:
        raise JournalUnavailable('submission journal not readable here and no public summary yet; '
                                 'the agent writes one on its next journal write')
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1_000_000:
        raise JournalUnavailable('invalid submission journal public summary')
    try:
        value = json.loads(target.read_bytes())
        if set(value) != _SUMMARY_KEYS or not isinstance(value['unresolved'], list):
            raise ValueError('shape')
        rows = [{k: r[k] for k in _ROW_KEYS} for r in value['unresolved']]
        if any(r['state'] not in ('reserved', 'prepared') for r in rows):
            raise ValueError('state')
    except (ValueError, TypeError, KeyError) as exc:
        raise JournalUnavailable('invalid submission journal public summary') from exc
    try:
        now = _journal_stat(path)
    except FileNotFoundError:
        raise JournalUnavailable('submission journal missing; reconcile storage')
    if now != value['journal']:
        raise JournalUnavailable('submission journal changed after its public summary; '
                                 'spending must remain blocked until the agent refreshes it')
    return rows


def refresh_public_summary(path: Path) -> bool:
    """Rewrite the summary for an EXISTING journal (the deploy runs this as the
    agent identity once, so a box whose journal has not been written since the
    upgrade shows its state). Never creates a journal. Returns True if written."""
    path = Path(path).absolute()
    if not _regular(path):
        return False
    with connection(path, write=True) as db:
        db.execute('SELECT 1').fetchone()
    return public_summary_path(path).is_file()


if __name__ == '__main__':
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == 'refresh':
        print('refreshed' if refresh_public_summary(Path(sys.argv[2])) else 'no journal')
    else:
        print('usage: python -m core.wallet.submission_store refresh <submissions.sqlite>')
        sys.exit(2)
