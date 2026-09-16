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
                    nonce TEXT NOT NULL, created REAL NOT NULL, state TEXT NOT NULL)''')
        identity, count = _validate(db, marker)
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
    finally:
        if db is not None:
            db.close()
        os.close(fd)
