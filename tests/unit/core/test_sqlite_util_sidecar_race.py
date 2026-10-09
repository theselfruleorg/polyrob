"""A sidecar another process is unlinking must not fail the open.

Prod 2026-10-08: once the data root stopped being sticky, the last connection in one
service UID unlinks `-wal`/`-shm` while the other service's `wal_connect` is between
`os.lstat` and its single-link check. The stat then reports `st_nlink == 0` and the
outbound dispatcher logged "SQLite sidecar must be a single-link regular file" every
2-3 minutes. A zero-link sidecar is a file being deleted, like FileNotFoundError; only
an extra link (an alias) or a non-regular file is refused.
"""
import os

import pytest

from core import sqlite_util


def _stat_with_links(real_lstat, suffix, nlink):
    def fake(path, *a, **kw):
        info = real_lstat(path, *a, **kw)
        if str(path).endswith(suffix):
            fields = list(info)
            fields[3] = nlink  # st_nlink
            return os.stat_result(fields)
        return info
    return fake


def _make_store(tmp_path):
    p = tmp_path / "s.db"
    sqlite_util.init_schema(str(p), "CREATE TABLE IF NOT EXISTS t (x)")
    (tmp_path / "s.db-wal").write_bytes(b"")
    return str(p)


def test_a_sidecar_being_unlinked_is_not_refused(tmp_path, monkeypatch):
    db = _make_store(tmp_path)
    monkeypatch.setattr(sqlite_util.os, "lstat", _stat_with_links(os.lstat, "-wal", 0))
    conn = sqlite_util.wal_connect(db)
    conn.close()


def test_a_hard_linked_sidecar_is_still_refused(tmp_path, monkeypatch):
    db = _make_store(tmp_path)
    monkeypatch.setattr(sqlite_util.os, "lstat", _stat_with_links(os.lstat, "-wal", 2))
    with pytest.raises(OSError, match="single-link"):
        sqlite_util.wal_connect(db)
