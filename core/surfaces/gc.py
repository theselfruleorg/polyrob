"""GC that never drops a binding with pending outbound (would orphan a queued reply)."""
import logging
from core.sqlite_util import execute_retry

logger = logging.getLogger(__name__)


def _pending_session_keys(queue) -> set:
    rows = execute_retry(queue.db_path,
        "SELECT DISTINCT session_key FROM outbound_queue WHERE state IN ('pending','inflight')",
        fetch="all") or []
    return {r["session_key"] for r in rows}


def purge_stale_safe(registry, queue, older_than_secs: float) -> int:
    """Purge stale chat<->session bindings, but never purge a binding that has
    pending or in-flight outbound rows — doing so would orphan a queued reply.

    If ``queue`` is None, falls back to the plain ``registry.purge_stale`` path
    (legacy / no outbound queue wired).
    """
    protected = _pending_session_keys(queue) if queue is not None else set()
    if not protected:
        return registry.purge_stale(older_than_secs)
    # delete only stale rows whose key is not protected
    placeholders = ",".join("?" for _ in protected)
    cutoff = "strftime('%s','now') - ?"
    sql = (f"DELETE FROM session_chat_map WHERE updated_at < {cutoff} "
           f"AND session_key NOT IN ({placeholders})")
    return execute_retry(registry.db_path, sql, (older_than_secs, *protected)) or 0
