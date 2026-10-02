"""GC that never drops a binding with pending outbound (would orphan a queued reply)."""
import logging
from core.sqlite_util import execute_retry

logger = logging.getLogger(__name__)


def _pending_session_keys(queue) -> set:
    rows = execute_retry(queue.db_path,
        "SELECT DISTINCT session_key FROM outbound_queue WHERE state IN ('pending','inflight')",
        fetch="all") or []
    return {r["session_key"] for r in rows}


#: Thread anchors (one correspondent row per outbound message) older than this
#: are pruned; a late reply still resolves on the address-level binding.
THREAD_ANCHOR_RETENTION_SEC = 90 * 86400


def prune_surface_stores(registry) -> None:
    """AC5: prune the per-message rows of the stores that live beside
    ``surfaces.db`` — the room-traffic events (``RoomCaps``, same file) and the
    correspondent thread anchors (``correspondents.db``, same directory). Both
    grew forever. Located from the registry's path, as bootstrap places them.
    Each prune fails open with a WARNING; a missing correspondents.db is not
    created just to prune it."""
    import os
    db_path = getattr(registry, "db_path", None)
    if not db_path:
        return
    try:
        from core.surfaces.room_caps import RoomCaps
        n = RoomCaps(db_path).prune()
        if n:
            logger.info("surface GC pruned %d room-traffic event(s)", n)
    except Exception as e:
        logger.warning("surface GC: room-caps prune failed: %s", e)
    try:
        corr_db = os.path.join(os.path.dirname(db_path) or ".", "correspondents.db")
        if os.path.exists(corr_db):
            from core.surfaces.correspondents import CorrespondentRegistry
            n = CorrespondentRegistry(corr_db).prune_thread_anchors(
                THREAD_ANCHOR_RETENTION_SEC)
            if n:
                logger.info("surface GC pruned %d correspondent thread anchor(s)", n)
    except Exception as e:
        logger.warning("surface GC: thread-anchor prune failed: %s", e)


def purge_stale_safe(registry, queue, older_than_secs: float) -> int:
    """Purge stale chat<->session bindings, but never purge a binding that has
    pending or in-flight outbound rows — doing so would orphan a queued reply.

    If ``queue`` is None, falls back to the plain ``registry.purge_stale`` path
    (legacy / no outbound queue wired).

    OB15: the same tick prunes the outbox's terminal rows (delivered/dead past
    their retention) — nothing else ever deleted them. Fail-open: a prune fault
    never stops the binding purge.
    """
    if queue is not None and hasattr(queue, "prune"):
        try:
            pruned = queue.prune()
            if pruned:
                logger.info("surface GC pruned %d terminal outbox row(s)", pruned)
        except Exception as e:
            logger.warning("surface GC: outbox prune failed: %s", e)
    prune_surface_stores(registry)
    protected = _pending_session_keys(queue) if queue is not None else set()
    if not protected:
        return registry.purge_stale(older_than_secs)
    # delete only stale rows whose key is not protected
    placeholders = ",".join("?" for _ in protected)
    cutoff = "strftime('%s','now') - ?"
    sql = (f"DELETE FROM session_chat_map WHERE updated_at < {cutoff} "
           f"AND session_key NOT IN ({placeholders})")
    return execute_retry(registry.db_path, sql, (older_than_secs, *protected)) or 0
