"""P4: Telegram update_id dedup over WAL SQLite (atomic CAS).

Telegram redelivers the same update on a webhook-ack timeout, so the inbound handler
must drop a repeat BEFORE the side-effecting steps (identify -> get_or_create_by_tg_id
writes; route_inbound -> create_session). A SELECT-then-INSERT check races when two
deliveries of the same update arrive concurrently; `INSERT OR IGNORE` is the atomic
compare-and-set — exactly one caller gets rowcount==1 (process), the rest get 0 (drop).

The store's default window is 5 minutes (Telegram's webhook retry horizon); the
harness passes ONE DAY (``harness._TG_DEDUP_WINDOW_S``) because the poll offset
lives in memory and a restart replays every unacknowledged update (TG5). Stale rows
are pruned before each check.
"""
from core.surfaces.idempotency import IdempotencyStore


class UpdateDedup(IdempotencyStore):
    """The surface-agnostic :class:`IdempotencyStore` over this surface's
    pre-existing table — ``tg_dedup.db`` on a running install already holds
    ``seen_updates(update_id, ts)``, so the table name is kept and the body is
    not a second copy."""
    table = "seen_updates"
    key_col = "update_id"
