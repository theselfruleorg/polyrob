"""Durable run taint. Only explicit owner ingress clears a row; age never does."""
from pathlib import Path
import sqlite3
import stat

from core.runtime_paths import data_home_db_path
from core.sqlite_util import init_schema, wal_connect

_SCHEMA = 'CREATE TABLE IF NOT EXISTS refusal_taints (session_id TEXT PRIMARY KEY)'


def path():
    return Path(data_home_db_path('refusal_taint.db')).absolute()


def read(key):
    file = path()
    try:
        info = file.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OSError('Refusal taint store must be a single-link regular file')
    db = sqlite3.connect(file.as_uri() + '?mode=ro', uri=True, timeout=.25)
    try:
        return db.execute('SELECT 1 FROM refusal_taints WHERE session_id=?', (key,)).fetchone() is not None
    finally:
        db.close()


def write(key, *, tainted):
    file = path()
    if not tainted and not file.exists():
        return
    init_schema(str(file), _SCHEMA, mkdir=True)
    db = wal_connect(str(file))
    try:
        with db:
            if tainted:
                db.execute('INSERT OR IGNORE INTO refusal_taints VALUES (?)', (key,))
            else:
                db.execute('DELETE FROM refusal_taints WHERE session_id=?', (key,))
    finally:
        db.close()
