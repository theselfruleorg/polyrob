"""x402 payment tables."""

from modules.database.connection import DatabaseConnection
import logging

logger = logging.getLogger('database.x402_tables')


async def dedupe_and_create_tx_hash_unique_index(db, log: logging.Logger) -> None:
    """Shared by `X402Tables.create_tables()` and migration v1.6.0
    (`migrations/versions/v1_6_0_x402_tx_hash_unique.py`) — the single
    application path for `idx_x402_requests_tx_hash_unique`.

    Boot-crash-hazard fix (Task 11 follow-up, Medium): a legacy DB that
    predates this index may already contain duplicate non-NULL
    `transaction_hash` rows (exactly the historical C2 replay bug this index
    guards against). `CREATE UNIQUE INDEX` over dirty data raises
    `sqlite3.IntegrityError`, which — with no per-table isolation in
    `DatabaseManager._init_tables()` — would crash app boot AND
    `python -m migrations.migrate upgrade`. So before creating the index:

    1. Find every `transaction_hash` shared by more than one row.
    2. KEEP the earliest row (by `created_at` ASC, `rowid` ASC tie-break —
       same ordering precedent as `invoicing.match_pending_invoice_by_amount`
       — the one that legitimately settled first).
    3. NULL OUT `transaction_hash` on the rest. Rows are NEVER deleted — they
       are financial records — only the duplicated tx stamp is cleared, and
       every affected `request_id` is named in a loud WARNING log: an
       operator-visible reconciliation signal (those invoices now read as
       "settled with an unrecorded tx" and need manual review).
    4. THEN create the partial unique index.

    Tolerant of a DB that doesn't have `x402_payment_requests` at all yet
    (`PRAGMA table_info` on a nonexistent table returns empty, not an error —
    same idiom the pre-existing migration tolerance used). If the index still
    cannot be created for any other reason, this logs loudly and returns
    without raising — degrade, never crash boot. Idempotent: a duplicate-free
    DB (including a fresh one) touches zero rows and just creates the index.
    """
    try:
        table_cols = await db.fetch_all("PRAGMA table_info(x402_payment_requests)")
    except Exception:
        table_cols = None
    if not table_cols:
        log.info(
            "  x402_payment_requests table not present yet — skipping "
            "tx_hash unique-index creation (self-heals once the table exists)"
        )
        return

    try:
        dup_groups = await db.fetch_all(
            """SELECT transaction_hash FROM x402_payment_requests
               WHERE transaction_hash IS NOT NULL
               GROUP BY transaction_hash
               HAVING COUNT(*) > 1"""
        )
        for group in dup_groups or []:
            tx_hash = group["transaction_hash"]
            rows = await db.fetch_all(
                """SELECT id FROM x402_payment_requests
                   WHERE transaction_hash = ?
                   ORDER BY created_at ASC, rowid ASC""",
                (tx_hash,),
            )
            if not rows or len(rows) < 2:
                continue
            winner_id = rows[0]["id"]
            loser_ids = [r["id"] for r in rows[1:]]
            for loser_id in loser_ids:
                await db.execute(
                    """UPDATE x402_payment_requests
                       SET transaction_hash = NULL, updated_at = datetime('now')
                       WHERE id = ?""",
                    (loser_id,),
                )
            log.warning(
                "x402 transaction_hash dedup (legacy duplicate-settlement "
                "data found before unique-index creation): tx_hash=%s was "
                "shared by %d rows; KEPT request_id=%s (earliest by "
                "created_at) and CLEARED transaction_hash on "
                "request_id(s)=%s — those invoices now need manual "
                "reconciliation (Task 11 boot-crash-hazard follow-up).",
                tx_hash, len(rows), winner_id, loser_ids,
            )
    except Exception as e:
        log.error(
            f"Error deduplicating x402_payment_requests.transaction_hash "
            f"prior to unique-index creation (continuing to attempt index "
            f"creation): {e}"
        )

    try:
        await db.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS idx_x402_requests_tx_hash_unique
            ON x402_payment_requests(transaction_hash)
            WHERE transaction_hash IS NOT NULL
        ''')
    except Exception as e:
        log.error(
            f"Could not create idx_x402_requests_tx_hash_unique even after "
            f"deduplication — degrading, NOT crashing boot. The "
            f"transaction_hash_already_settled pre-check in "
            f"modules.x402.invoicing.settle_payment_request remains the "
            f"primary settlement-replay guard regardless: {e}"
        )


async def dedupe_and_create_subscription_pending_unique_index(db, log: logging.Logger) -> None:
    """Shared by `X402Tables.create_tables()` and migration v1.7.0
    (`migrations/versions/v1_7_0_x402_subscription_pending_unique.py`) — the
    single application path for
    `idx_x402_requests_pending_subscription_unique`.

    Task 14 review fix (Important, duplicate-renewal TOCTOU): the "no open
    pending renewal invoice for this subscription" check in
    `modules.x402.subscriptions._has_open_renewal_invoice` is a plain SELECT
    with no atomic guard before the later INSERT in
    `modules.x402.invoicing.create_payment_request` — under
    `UVICORN_WORKERS>1`, two concurrent settlement-watcher processes could
    both observe "no open invoice" and both create a renewal invoice for the
    SAME subscription in one tick window; if both later settle, `paid_through`
    extends TWICE for one period.

    Mirrors `dedupe_and_create_tx_hash_unique_index` EXACTLY (same boot-crash-
    hazard shape, Task 11 precedent): a legacy DB may already contain more
    than one PENDING invoice carrying the same `metadata.subscription_id`
    (exactly the race this index closes). `CREATE UNIQUE INDEX` over dirty
    data raises `sqlite3.IntegrityError`, which — with no per-table isolation
    in `DatabaseManager._init_tables()` — would crash app boot AND
    `python -m migrations.migrate upgrade`. So before creating the index:

    1. Find every (still-)pending `metadata.subscription_id` shared by more
       than one row.
    2. KEEP the earliest row (by `created_at` ASC, `rowid` ASC tie-break —
       same ordering precedent as `dedupe_and_create_tx_hash_unique_index` /
       `modules.x402.invoicing.match_pending_invoice_by_amount`) — the FIRST
       renewal invoice actually created for that period.
    3. NULL OUT `metadata.subscription_id` (via SQLite JSON1's
       `json_set(metadata, '$.subscription_id', NULL)`) on the rest. Rows are
       NEVER deleted, and their `status`/amount/everything else is left
       untouched — they stay pending, payable invoices; they simply stop
       being counted as "the" open renewal for that subscription (and drop
       out of the unique index's partial scope, since the index's WHERE
       clause requires `subscription_id` to be present). Every affected
       `request_id` is named in a loud WARNING log — an operator-visible
       reconciliation signal (an already-sent renewal notice referencing that
       request_id is still payable; it just won't auto-apply to the
       subscription's paid_through on settlement).
    4. THEN create the partial unique index.

    Tolerant of a DB that doesn't have `x402_payment_requests` at all yet. If
    the index still cannot be created for any other reason, this logs loudly
    and returns without raising — degrade, never crash boot. Idempotent: a
    duplicate-free DB (including a fresh one) touches zero rows and just
    creates the index.
    """
    try:
        table_cols = await db.fetch_all("PRAGMA table_info(x402_payment_requests)")
    except Exception:
        table_cols = None
    if not table_cols:
        log.info(
            "  x402_payment_requests table not present yet — skipping "
            "subscription pending-unique index creation (self-heals once "
            "the table exists)"
        )
        return

    try:
        dup_groups = await db.fetch_all(
            """SELECT json_extract(metadata, '$.subscription_id') AS sub_id
               FROM x402_payment_requests
               WHERE status = 'pending'
                 AND json_extract(metadata, '$.subscription_id') IS NOT NULL
               GROUP BY sub_id
               HAVING COUNT(*) > 1"""
        )
        for group in dup_groups or []:
            sub_id = group["sub_id"]
            rows = await db.fetch_all(
                """SELECT id FROM x402_payment_requests
                   WHERE status = 'pending'
                     AND json_extract(metadata, '$.subscription_id') = ?
                   ORDER BY created_at ASC, rowid ASC""",
                (sub_id,),
            )
            if not rows or len(rows) < 2:
                continue
            winner_id = rows[0]["id"]
            loser_ids = [r["id"] for r in rows[1:]]
            for loser_id in loser_ids:
                await db.execute(
                    """UPDATE x402_payment_requests
                       SET metadata = json_set(metadata, '$.subscription_id', NULL),
                           updated_at = datetime('now')
                       WHERE id = ?""",
                    (loser_id,),
                )
            log.warning(
                "x402 subscription pending-renewal dedup (legacy duplicate "
                "renewal-invoice data found before unique-index creation): "
                "subscription_id=%s was carried by %d PENDING rows; KEPT "
                "request_id=%s (earliest by created_at) and CLEARED "
                "metadata.subscription_id on request_id(s)=%s — those "
                "invoices remain pending/payable but no longer count as the "
                "subscription's open renewal (Task 14 boot-crash-hazard "
                "follow-up); manual reconciliation recommended.",
                sub_id, len(rows), winner_id, loser_ids,
            )
    except Exception as e:
        log.error(
            f"Error deduplicating x402_payment_requests pending subscription "
            f"renewals prior to unique-index creation (continuing to attempt "
            f"index creation): {e}"
        )

    try:
        await db.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS idx_x402_requests_pending_subscription_unique
            ON x402_payment_requests(json_extract(metadata, '$.subscription_id'))
            WHERE status = 'pending'
              AND json_extract(metadata, '$.subscription_id') IS NOT NULL
        ''')
    except Exception as e:
        log.error(
            f"Could not create idx_x402_requests_pending_subscription_unique "
            f"even after deduplication — degrading, NOT crashing boot. "
            f"Without this index, modules.x402.subscriptions."
            f"_has_open_renewal_invoice's plain SELECT remains the ONLY "
            f"(non-atomic) guard against a duplicate concurrent renewal "
            f"invoice: {e}"
        )


class X402Tables:
    """Manage x402 payment tables."""

    def __init__(self, db: DatabaseConnection):
        self.db = db
        self.logger = logging.getLogger('database.x402_tables')

    async def create_tables(self) -> None:
        """Create x402 tables."""

        try:
            # x402 payment requests
            await self.db.execute('''
                CREATE TABLE IF NOT EXISTS x402_payment_requests (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    payer_address TEXT,
                    amount TEXT NOT NULL,
                    amount_usd REAL NOT NULL,
                    asset TEXT NOT NULL,
                    chain TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    nonce TEXT UNIQUE NOT NULL,
                    deadline INTEGER NOT NULL,
                    status TEXT DEFAULT 'pending',
                    transaction_hash TEXT,
                    payment_id TEXT,
                    metadata TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES user_profiles(user_id) ON DELETE SET NULL
                )
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_x402_requests_nonce
                ON x402_payment_requests(nonce)
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_x402_requests_status
                ON x402_payment_requests(status)
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_x402_requests_payer
                ON x402_payment_requests(payer_address)
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_x402_requests_user
                ON x402_payment_requests(user_id)
            ''')

            # NOTE (G-42, dead-code cleanup): an `x402_access_log` table used
            # to be created here (id/payment_request_id/payer_address/
            # endpoint/method/response_status/accessed_at + two indices).
            # Nothing ever INSERTed into it or read from it — verified dead
            # DDL, not an unwired feature with a concrete consumer — so it was
            # removed rather than wired to reduce schema surface. Mirrored in
            # modules/database/schema.sql.

            # On-chain settlement-scan checkpoint (Task 11, Phase 2): one row
            # per treasury address, tracking the last fully-scanned USDC
            # Transfer-log block so `SettlementWatcher` never rescans history
            # and never re-detects an already-processed transfer.
            await self.db.execute('''
                CREATE TABLE IF NOT EXISTS settlement_scan (
                    treasury TEXT PRIMARY KEY,
                    last_block INTEGER NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Watchtower subscriptions (Task 14, Phase 3 R5): a prepaid-period
            # billing row gating a cron job's continued firing. Self-healing,
            # same pattern as settlement_scan above — no dedicated migration
            # entry needed for a brand-new, always-empty-until-used table.
            # See modules/x402/subscriptions.py for the store + lifecycle.
            await self.db.execute('''
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    correspondent_surface TEXT NOT NULL,
                    correspondent_address TEXT NOT NULL,
                    cron_job_id TEXT NOT NULL,
                    amount_usd REAL NOT NULL,
                    period_days INTEGER NOT NULL DEFAULT 30,
                    paid_through INTEGER NOT NULL,
                    renewal_lead_days INTEGER NOT NULL DEFAULT 5,
                    grace_days INTEGER NOT NULL DEFAULT 3,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_subscriptions_user
                ON subscriptions(user_id)
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_subscriptions_status
                ON subscriptions(status)
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_subscriptions_cron_job
                ON subscriptions(cron_job_id)
            ''')

            # One row per settled renewal invoice ACTUALLY applied to a
            # subscription's paid_through — the idempotency key so a
            # re-processed settlement (watcher retry, restart mid-tick) can
            # never double-extend a subscription's paid-through date. The
            # PRIMARY KEY on request_id is the enforcement mechanism: a
            # second INSERT for the same request_id raises IntegrityError,
            # which modules.x402.subscriptions.apply_settlement treats as
            # "already applied" (mirrors the transaction_hash replay guard
            # above), never as an error.
            await self.db.execute('''
                CREATE TABLE IF NOT EXISTS subscription_applied_settlements (
                    request_id TEXT PRIMARY KEY,
                    subscription_id TEXT NOT NULL,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Task 11 review fix C2: a given on-chain tx must settle AT MOST
            # ONE invoice EVER — a replayed/re-scanned transfer (e.g. after a
            # mid-batch watcher failure left the scan checkpoint un-advanced)
            # must never be reapplied to a DIFFERENT, now-unrelated same-
            # amount invoice. Partial (NULL-exempt: pending/expired rows never
            # carry a transaction_hash, and many may legitimately share NULL)
            # so this only constrains rows that HAVE actually settled on-chain.
            # `modules.x402.invoicing.settle_payment_request` pre-checks this
            # invariant explicitly; this index is defense-in-depth against a
            # genuine concurrent-write race. Mirrored by migration v1.6.0 for
            # explicit schema-version tracking.
            #
            # Boot-crash-hazard follow-up (Medium): a legacy DB may already
            # hold duplicate non-NULL transaction_hash rows (the historical C2
            # bug this index guards against), and CREATE UNIQUE INDEX over
            # dirty data raises sqlite3.IntegrityError — with no per-table
            # isolation in DatabaseManager._init_tables(), that would crash
            # app boot. `dedupe_and_create_tx_hash_unique_index` deduplicates
            # (keeps the earliest row, clears the tx stamp on the rest, never
            # deletes) before creating the index, and degrades (loud log, no
            # raise) if the index still can't be created.
            await dedupe_and_create_tx_hash_unique_index(self.db, self.logger)

            # Task 14 review fix (Important, duplicate-renewal TOCTOU): at
            # most one PENDING invoice per subscription_id — see
            # `dedupe_and_create_subscription_pending_unique_index`'s
            # docstring. Mirrored by migration v1.7.0 for explicit
            # schema-version tracking.
            await dedupe_and_create_subscription_pending_unique_index(self.db, self.logger)
            # NOTE (I1, deliberately NOT a DB constraint): the amount-collision
            # jitter dedupe (`modules.x402.invoicing._dedupe_amount_for_treasury`)
            # only applies to `create_payment_request` when on-chain detection
            # is on (`_jitter_should_apply`) — with detection off, two PENDING
            # invoices at the exact same (recipient, amount_usd) are
            # INTENTIONALLY allowed (byte-identical legacy behavior; no
            # on-chain ambiguity risk exists to disambiguate). A DB-level
            # unique index here would enforce uniqueness unconditionally,
            # which is unsafe/wrong for that flag-off case. The fix instead
            # closes the concurrent-create TOCTOU with an in-process
            # per-treasury `asyncio.Lock` around the dedupe-SELECT + INSERT
            # span, scoped to the SAME jitter-active gate — see
            # `invoicing._treasury_lock` / `invoicing.create_payment_request`.

            self.logger.info("✅ x402 tables created successfully")

        except Exception as e:
            self.logger.error(f"Error creating x402 tables: {e}")
            raise
