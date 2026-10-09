"""Hard per-tenant logical storage ceilings; refusal never deletes old data."""

MAX_ROWS = 10_000
MAX_BYTES = 32 * 1024 * 1024
MAX_ROW_BYTES = 64 * 1024
# The auto-written cross-session ``memories`` store is short per-turn narration
# (MEMORY_ROW_MAX_CHARS) that ages out under MEMORY_RETENTION_DAYS, so its row
# count grows far faster than its bytes: prod held 24k rows / 8.9 MB when the
# 10k-row bound refused every turn sync. MAX_BYTES stays the disk bound.
MAX_MEMORY_ROWS = 100_000


def _row_cap(table: str) -> int:
    return MAX_MEMORY_ROWS if table == "memories" else MAX_ROWS


def valid_kb_metadata(collection, source_path, source_hash, mime) -> bool:
    return all(isinstance(value, str) and len(value.encode("utf-8")) <= limit
               for value, limit in ((collection, 128), (source_path, 4096),
                                    (source_hash, 128), (mime, 128)))


def check_fts_insert(conn, table: str, user_id: str, contents) -> None:
    """Call inside the writer's BEGIN IMMEDIATE transaction (FTS forbids triggers)."""
    if table not in {"memories", "kb_chunks"}:
        raise ValueError("Unsupported memory resource")
    sizes = [len(text.encode("utf-8")) for text in contents]
    if any(size > MAX_ROW_BYTES for size in sizes):
        raise ValueError("Memory row exceeds the storage limit")
    count, size = conn.execute(
        f"SELECT count(*), COALESCE(sum(length(CAST(content AS BLOB))), 0) "
        f"FROM {table} WHERE user_id=?", (user_id,)).fetchone()
    if count + len(sizes) > _row_cap(table) or size + sum(sizes) > MAX_BYTES:
        raise ValueError("Tenant memory storage limit reached; remove old content before adding more")


def insert_finding(db_path, user_id: str, session_id: str, content: str) -> int:
    """Reserve capacity and insert under the same SQLite writer lock."""
    from core.sqlite_util import wal_connect
    conn = wal_connect(db_path, timeout=5)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            check_fts_insert(conn, "memories", user_id, (content,))
            return conn.execute(
                "INSERT INTO memories (user_id, session_id, content) VALUES (?, ?, ?)",
                (user_id, session_id, content)).lastrowid
    finally:
        conn.close()


def install_regular_limits(conn) -> None:
    """SQLite enforces episode/note limits for all writers, including updates."""
    resources = {
        "episodes": ("task", "summary", "artifacts", "meta", "thread_key", "goal_id", "kind", "outcome"),
        "curated_memory": ("content", "title", "tags", "links", "source"),
    }
    for table, columns in resources.items():
        size = " + ".join(f"length(CAST(COALESCE({column}, '') AS BLOB))" for column in columns)
        new_size = " + ".join(f"length(CAST(COALESCE(NEW.{column}, '') AS BLOB))" for column in columns)
        conn.execute(f"CREATE INDEX IF NOT EXISTS {table}_quota_user ON {table}(user_id)")
        for operation in ("INSERT", "UPDATE"):
            # An episode upsert replaces the same (tenant, session) row; it
            # consumes one slot even when the tenant is exactly at the cap.
            excluded = ("session_id=NEW.session_id" if table == "episodes" else "id=NEW.id")
            where = f"user_id=NEW.user_id AND NOT ({excluded})"
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_quota_{operation.lower()} "
                         f"BEFORE {operation} ON {table} BEGIN "
                         f"SELECT CASE WHEN ({new_size}) > {MAX_ROW_BYTES} "
                         f"OR (SELECT count(*) FROM {table} WHERE {where}) + 1 > {MAX_ROWS} "
                         f"OR (SELECT COALESCE(sum({size}),0) FROM {table} WHERE {where}) "
                         f"+ ({new_size}) > {MAX_BYTES} "
                         "THEN RAISE(ABORT, 'Tenant memory storage limit reached') END; END")
