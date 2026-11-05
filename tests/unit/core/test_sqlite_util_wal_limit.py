"""core.sqlite_util.wal_connect bounds the -wal file a connection leaves behind."""
def test_wal_connect_limits_the_retained_wal(tmp_path):
    """A checkpoint on an open connection left the -wal file at its peak size."""
    from core.sqlite_util import wal_connect
    conn = wal_connect(str(tmp_path / "a.db"))
    try:
        assert conn.execute("PRAGMA journal_size_limit").fetchone()[0] == 67108864
    finally:
        conn.close()


def test_the_wal_shrinks_to_the_limit_when_the_log_restarts(tmp_path, monkeypatch):
    import os
    import core.sqlite_util as su
    limit = 256 * 1024
    monkeypatch.setattr(su, "_WAL_SIZE_LIMIT_BYTES", limit)
    db = str(tmp_path / "a.db")
    conn = su.wal_connect(db)
    try:
        conn.execute("CREATE TABLE t (b BLOB)")
        conn.execute("INSERT INTO t VALUES (?)", (b"x" * (2 * 1024 * 1024),))
        conn.commit()
        assert os.path.getsize(db + "-wal") > limit
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall()
        conn.execute("INSERT INTO t VALUES (?)", (b"y",))
        conn.commit()
        assert os.path.getsize(db + "-wal") <= limit
    finally:
        conn.close()
