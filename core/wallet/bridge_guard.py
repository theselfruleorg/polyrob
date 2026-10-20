"""Two-phase guard for a cross-chain bridge (proposal 037).

`tx_guard` proves a trade by asserting balance deltas INSIDE one transaction.
`routes/lifi.py` refuses cross-chain for exactly that reason, in as many words:

    "a bridge adds a settlement-delay failure mode `tx_guard` has no model for
     (the value leaves on one chain and arrives later on another, so there is no
     single transaction whose deltas can be asserted)."

That is a true statement about a ONE-phase guard. This module is the model it
says is missing — not a relaxation of it.

    Phase 1 (origin chain, BEFORE broadcast)
        The existing single-transaction discipline, unchanged: the declared
        amount, the recipient, the arrival floor and the destination chain are
        asserted against the quote, and the quote against what we asked for.
        Nothing here is new; it is `tx_guard`'s bar applied to the send.

    Phase 2 (destination chain, AFTER broadcast)
        The ARRIVAL is the assertion the single-transaction guard cannot make.
        Ground truth is the recipient's measured balance on the destination
        chain, not the provider's status field — a status is a third party's
        claim about our money. The provider's status is read too, but only to
        tell "not yet" from "refunded"; it can never, by itself, report success.

HONESTY RULES, in order of how badly each one bites:

1.  A bridge that has not arrived by the deadline is **`in_flight`** — neither
    success nor failure. Reporting it as failure invites a re-send, which on a
    bridge is how you pay twice. Reporting it as success is a lie about money.
    It is recorded durably and escalated to the owner instead.
2.  An UNREADABLE destination balance is **unknown**, never zero. A zero read
    from a dead RPC is indistinguishable from funds that never arrived, and the
    second one is an incident.
3.  The record is written BEFORE the broadcast, not after. A crash between
    broadcast and record is precisely when an in-flight bridge is invisible, and
    an invisible in-flight bridge is an unrecoverable one.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

#: How long phase 2 waits for arrival before recording `in_flight` and handing
#: the question to the owner. Relay's own estimates are 1-2s for the routes we
#: use; this is two orders of magnitude of headroom, and it still has to END.
DEFAULT_ARRIVAL_DEADLINE_SEC = 300

#: Gap between arrival polls. Short enough that a 1-2s settlement is reported
#: promptly, long enough not to hammer a public RPC for five minutes.
ARRIVAL_POLL_SEC = 5.0

#: Relay's native-asset sentinel on an EVM chain. Lower-cased here because it
#: is compared against a quote field, and 0x-hex folds case.
_NATIVE_EVM = "0x0000000000000000000000000000000000000000"

#: Relay's native-SOL sentinel (the System program id). base58: exact match.
_NATIVE_SVM = "11111111111111111111111111111111"

STATE_PENDING = "pending"
STATE_ARRIVED = "arrived"
STATE_IN_FLIGHT = "in_flight"
STATE_FAILED = "failed"
#: L-6: a ``pending`` row with no transaction, older than
#: :data:`UNSENT_STALE_SEC`. A crash between ``record_pending`` and the
#: broadcast leaves one, and the provider never reports ``failure`` for an
#: order that was never funded, so without this it blocks its (dest, asset)
#: balance forever. Terminal: it is not in the open set.
STATE_NEVER_SENT = "never_sent"
UNSENT_STALE_SEC = 3600

#: B-1: how far before the bridge row's ``created_at`` a delivering
#: transaction's block time may be and still count (clock skew between this
#: host and the chain). Anything older is an EARLIER transfer, not this one.
PROOF_SKEW_SEC = 120

#: The proof basis recorded when Relay reports success with no destination tx
#: hash on an EVM chain and the arrival rests on the measured balance rise.
BASIS_BALANCE_ONLY = "balance_only"

#: ERC-20 ``Transfer(address,address,uint256)``.
_TRANSFER_TOPIC = ("0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bridges (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    request_id      TEXT NOT NULL,
    origin_chain_id INTEGER NOT NULL,
    dest_chain_id   INTEGER NOT NULL,
    recipient       TEXT NOT NULL,
    currency_out    TEXT NOT NULL,
    amount_in_raw   TEXT NOT NULL,
    min_out_raw     TEXT NOT NULL,
    amount_usd      REAL,
    state           TEXT NOT NULL DEFAULT 'pending',
    tx_ref          TEXT,
    balance_before  TEXT,
    balance_after   TEXT,
    detail          TEXT,
    created_at      REAL NOT NULL,
    settled_at      REAL,
    -- When the owner was last told this row is still unresolved. Without it the
    -- watcher would re-escalate the same stuck bridge on every tick, and an alert
    -- that repeats is an alert that gets muted.
    escalated_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_bridges_state ON bridges(state, user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bridges_request ON bridges(request_id);
-- B-1: every destination transaction that proved a bridge's arrival, claimed by
-- exactly ONE bridge row. The UNIQUE index is the rule: a hash another row has
-- already claimed can never prove a second arrival (an earlier delivery, a
-- provider that repeats a hash). CREATE ... IF NOT EXISTS: an existing store
-- gains the table in place on its next `_init`.
CREATE TABLE IF NOT EXISTS bridge_proofs (
    dest_chain_id   INTEGER NOT NULL,
    tx_hash         TEXT NOT NULL,
    bridge_id       TEXT NOT NULL,
    claimed_at      REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bridge_proofs_hash
    ON bridge_proofs(dest_chain_id, tx_hash);
"""


def bridges_db_path() -> str:
    """`<data_home>/bridges.db`. Resolved at CALL time, never bound at import
    (`tests/test_home_binding_ratchet.py`)."""
    import os
    from core.runtime_paths import resolve_data_home
    return os.path.join(str(resolve_data_home()), "bridges.db")


def _init(db_path: str) -> None:
    # Rows written before ``escalated_at`` existed gain it in place
    # (``ensure_columns``: only a duplicate-column race is swallowed).
    from core.sqlite_util import init_schema
    init_schema(db_path, _SCHEMA, columns={"bridges": (("escalated_at", "REAL"),)})


@dataclass(frozen=True)
class Phase1Verdict:
    ok: bool
    reason: str = ""


def assert_phase1(*, quote, expected_recipient: str, declared_amount_raw: int,
                  dest_chain_name: Optional[str]) -> Phase1Verdict:
    """Everything that must hold BEFORE a single lamport or wei is broadcast.

    The quote object has already asserted itself against the request inside
    `RelayBridgeProvider._parse_quote` (chains, currencies, sender, recipient,
    amount, decimals, a positive arrival floor, exactly one signable step). This
    function is the WALLET's half — the assertions that depend on who we are and
    what the chain registry says, which a provider must never be trusted to make
    about us.
    """
    from core.wallet import chains as _chains

    if not _same(quote.recipient, expected_recipient):
        return Phase1Verdict(False, (
            f"REFUSED: the order pays {quote.recipient}, which is not this "
            f"wallet's address on the destination chain ({expected_recipient}). "
            f"A bridge to an address we do not control is an irreversible "
            f"transfer, not a bridge."))

    if int(quote.amount_in_raw) > int(declared_amount_raw):
        return Phase1Verdict(False, (
            f"REFUSED: the order consumes {quote.amount_in_raw}, more than the "
            f"declared {declared_amount_raw}."))

    if int(quote.min_out_raw) <= 0:
        return Phase1Verdict(False, (
            "REFUSED: the arrival floor is not positive, so phase 2 would "
            "assert nothing. A guard that cannot fail is not a guard."))

    # The destination must be a chain this wallet actually knows how to read a
    # balance on. Without that, phase 2 degrades to trusting the provider's
    # status field — which is the exact trust this module exists to avoid.
    if not dest_chain_name:
        return Phase1Verdict(False, (
            f"REFUSED: destination chain id {quote.dest_chain_id} is not in the "
            f"pinned chain registry, so the arrival cannot be measured. "
            f"Nothing was broadcast."))
    row = _chains.get(dest_chain_name)
    if row is None:
        return Phase1Verdict(False, (
            f"REFUSED: chain {dest_chain_name!r} is not in the registry."))
    if getattr(row, "family", "evm") == "svm":
        # The registry has no EIP-155 id for an SVM chain (`chain_id=0`); the
        # provider's pseudo id is matched through the alias it registered.
        if arrival_chain_name(quote.dest_chain_id) != dest_chain_name:
            return Phase1Verdict(False, (
                f"REFUSED: the order targets chain id {quote.dest_chain_id}, "
                f"which does not resolve to {dest_chain_name!r}."))
    elif int(getattr(row, "chain_id", -1)) != int(quote.dest_chain_id):
        return Phase1Verdict(False, (
            f"REFUSED: registry chain {dest_chain_name!r} is id "
            f"{getattr(row, 'chain_id', None)}, but the order targets "
            f"{quote.dest_chain_id}."))

    return Phase1Verdict(True, "phase 1 assertions passed")


def chain_name_for_id(chain_id: Optional[int]) -> Optional[str]:
    """The pinned registry name for a chain id, or None if it names nothing.

    ⚠️ A falsey id is NOT Solana. Solana's registry row carries ``chain_id=0``
    because EIP-155 has no Solana analogue, and ``0`` is also what a missing
    column, an empty string and ``int(None or 0)`` read as — so a scan that
    matched it would resolve "I do not know" to a real chain and then read a
    balance, or build an explorer link, on the wrong one.

    ⚠️ This resolves REGISTRY ids only. The id a bridge row stores for a Solana
    origin is the PROVIDER's own pseudo chain id (Relay's), which is not a chain
    identifier this tier knows or should learn: ``core`` may not import
    ``tools`` (``tests/test_layering_ratchet.py``), and a copy of the constant
    here would be a second place for it to drift. A seat that renders a bridge
    ROW resolves that one itself, above this tier.
    """
    try:
        wanted = int(chain_id)
    except (TypeError, ValueError):
        return None
    if not wanted:
        return None
    from core.wallet import chains as _chains
    for name in _chains.names():
        row = _chains.get(name)
        if row is not None and int(getattr(row, "chain_id", -1)) == wanted:
            return name
    return None


def arrival_chain_name(chain_id) -> Optional[str]:
    """The registry name of a bridge DESTINATION id: a registry id, or a
    provider pseudo id the provider registered (``bridge_status.chain_alias``,
    Solana). Origins keep :func:`chain_name_for_id` — see its note."""
    name = chain_name_for_id(chain_id)
    if name:
        return name
    from core.wallet import bridge_status
    return bridge_status.chain_alias(chain_id)


def record_pending(*, user_id: str, quote, amount_usd: Optional[float],
                   balance_before: Optional[int], db_path: Optional[str] = None) -> str:
    """Write the in-flight row BEFORE broadcast. Returns the bridge id.

    Before, not after: the window between a broadcast and its record is exactly
    when a crash makes an in-flight bridge invisible, and an invisible in-flight
    bridge cannot be recovered by anyone.

    ⚠️ A plain ``INSERT`` (B-3). The request id comes from the quote API, and
    ``INSERT OR REPLACE`` on its UNIQUE index silently DELETED the existing row
    when a request id came back twice — losing an in-flight bridge. A reused id
    now raises ``sqlite3.IntegrityError`` and the caller refuses BEFORE broadcast.
    """
    from core.sqlite_util import execute_retry
    path = db_path or bridges_db_path()
    _init(path)
    bid = uuid.uuid4().hex[:16]
    execute_retry(path, (
        "INSERT INTO bridges (id, user_id, request_id, origin_chain_id, "
        "dest_chain_id, recipient, currency_out, amount_in_raw, min_out_raw, "
        "amount_usd, state, balance_before, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"),
        (bid, str(user_id), quote.request_id, int(quote.origin_chain_id),
         int(quote.dest_chain_id), quote.recipient, quote.currency_out,
         str(quote.amount_in_raw), str(quote.min_out_raw),
         amount_usd, STATE_PENDING,
         None if balance_before is None else str(balance_before), time.time()))
    return bid


def settle(bid: str, *, state: str, detail: str = "", tx_ref: Optional[str] = None,
           balance_after: Optional[int] = None, db_path: Optional[str] = None) -> None:
    """Move a recorded bridge to a terminal-or-parked state. Fail-open."""
    from core.sqlite_util import execute_retry
    path = db_path or bridges_db_path()
    try:
        execute_retry(path, (
            "UPDATE bridges SET state=?, detail=?, tx_ref=COALESCE(?, tx_ref), "
            "balance_after=?, settled_at=? WHERE id=?"),
            (state, detail[:2000], tx_ref,
             None if balance_after is None else str(balance_after),
             time.time(), bid))
    except Exception:
        logger.warning("bridge %s: could not record state %s", bid, state, exc_info=True)


def created_at_of(bid: str, *, db_path: Optional[str] = None) -> Optional[float]:
    """The row's ``created_at`` (the B-1 time bound), or None when unreadable —
    which leaves the bound off rather than refusing a real arrival."""
    from core.sqlite_util import execute_retry
    try:
        row = execute_retry(db_path or bridges_db_path(),
                            "SELECT created_at FROM bridges WHERE id=?", (bid,),
                            fetch="one")
        return None if row is None else float(row[0])
    except Exception:
        logger.warning("bridge %s: could not read created_at", bid, exc_info=True)
        return None


def mark_sent(bid: str, tx_ref: str, *, db_path: Optional[str] = None) -> None:
    """Stamp the origin transaction on a still-``pending`` row the moment it is
    broadcast. Without it a crash during phase 2 leaves a ``pending`` row with no
    tx, which the L-6 sweep would close as never sent. Fail-open."""
    from core.sqlite_util import execute_retry
    try:
        execute_retry(db_path or bridges_db_path(),
                      "UPDATE bridges SET tx_ref=COALESCE(tx_ref, ?) WHERE id=?",
                      (str(tx_ref), bid))
    except Exception:
        logger.warning("bridge %s: could not stamp the origin tx", bid, exc_info=True)


def close_stale_unsent(*, status: Optional[Callable[[str], tuple]] = None,
                       now: Optional[float] = None,
                       db_path: Optional[str] = None,
                       closed: Optional[list] = None) -> int:
    """Close every ``pending`` row with no tx older than :data:`UNSENT_STALE_SEC`
    as :data:`STATE_NEVER_SENT` (bridge L-6). Returns how many were closed.

    A row whose provider status reads ``success`` is left alone — that order WAS
    funded, whatever our record says. Never creates the store; fail-open.

    ``closed``: when given, each row THIS call closed is appended (with its
    ``detail``), so the caller sends the owner exactly one notice per row —
    the conditional UPDATE means no two callers close the same row."""
    import os

    from core.sqlite_util import execute_retry
    path = db_path or bridges_db_path()
    if not os.path.isfile(path):
        return 0
    cutoff = (now if now is not None else time.time()) - UNSENT_STALE_SEC
    try:
        _migrate_existing(path)
        rows = execute_retry(path, (
            "SELECT id, user_id, request_id, origin_chain_id, dest_chain_id, "
            "amount_usd FROM bridges WHERE state=? AND "
            "(tx_ref IS NULL OR tx_ref='') AND created_at<?"),
            (STATE_PENDING, cutoff), fetch="all") or []
    except Exception:
        logger.warning("bridge store: stale-row sweep could not read", exc_info=True)
        return 0
    count = 0
    for r in rows:
        row = dict(r) if not isinstance(r, dict) else r
        if status is not None:
            try:
                state, _detail = status(str(row.get("request_id") or ""))
            except Exception:
                state = "unknown"
            if state == "success":
                continue
        detail = (f"no origin transaction was recorded within "
                  f"{UNSENT_STALE_SEC // 60} minutes of the record; closed as "
                  f"never sent. If the provider later shows a deposit for this "
                  f"request id, check it before any re-send.")
        try:
            n = execute_retry(path, (
                "UPDATE bridges SET state=?, detail=?, settled_at=? WHERE id=? "
                "AND state=? AND (tx_ref IS NULL OR tx_ref='')"),
                (STATE_NEVER_SENT, detail, time.time(), row["id"], STATE_PENDING))
            count += int(n or 0)
            if n and closed is not None:
                closed.append(dict(row, state=STATE_NEVER_SENT, detail=detail))
        except Exception:
            logger.warning("bridge %s: could not close as never sent", row.get("id"),
                           exc_info=True)
    return count


def _hash_key(tx_hash: str) -> str:
    """0x-hex folds case; base58 is case-SENSITIVE and stays exact."""
    h = str(tx_hash).strip()
    return h.lower() if h[:2].lower() == "0x" else h


def proof_claimed_by_other(bid: Optional[str], dest_chain_id, tx_hash: str, *,
                           db_path: Optional[str] = None) -> bool:
    """Has a DIFFERENT bridge row already claimed ``tx_hash`` as its arrival?
    An unreadable store answers True: a claim we cannot rule out does not
    count."""
    import os

    from core.sqlite_util import execute_retry
    path = db_path or bridges_db_path()
    if not os.path.isfile(path):
        return False
    try:
        _migrate_existing(path)
        row = execute_retry(path, (
            "SELECT bridge_id FROM bridge_proofs WHERE dest_chain_id=? AND tx_hash=?"),
            (int(dest_chain_id or 0), _hash_key(tx_hash)), fetch="one")
    except Exception:
        logger.warning("bridge store: proof claim unreadable", exc_info=True)
        return True
    if row is None:
        return False
    owner = row["bridge_id"] if not isinstance(row, tuple) else row[0]
    return owner != bid


def claim_proof(bid: str, dest_chain_id, hashes, *,
                db_path: Optional[str] = None) -> bool:
    """Claim every hash in ``hashes`` for ``bid`` in ONE transaction. False if
    any of them is already another row's (the UNIQUE index decides, so two
    concurrent provers cannot both win) or the store cannot be written."""
    import sqlite3

    from core.sqlite_util import wal_connect
    path = db_path or bridges_db_path()
    try:
        _init(path)
        conn = wal_connect(path)
    except Exception:
        logger.warning("bridge %s: proof store unwritable", bid, exc_info=True)
        return False
    try:
        conn.execute("BEGIN IMMEDIATE")
        for h in hashes:
            key = _hash_key(h)
            cur = conn.execute(
                "SELECT bridge_id FROM bridge_proofs WHERE dest_chain_id=? AND tx_hash=?",
                (int(dest_chain_id or 0), key)).fetchone()
            if cur is not None:
                if cur[0] != bid:
                    conn.rollback()
                    return False
                continue
            conn.execute(
                "INSERT INTO bridge_proofs (dest_chain_id, tx_hash, bridge_id, "
                "claimed_at) VALUES (?,?,?,?)",
                (int(dest_chain_id or 0), key, bid, time.time()))
        conn.commit()
        return True
    except sqlite3.Error:
        logger.warning("bridge %s: proof claim failed", bid, exc_info=True)
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        conn.close()


def _migrate_existing(path: str) -> None:
    """Bring an EXISTING store up to the current schema. Never creates one.

    Caught on the 2026-09-12 prod deploy: `escalated_at` was added inside
    `_init`, which only the WRITE path calls, so the readers selected a column
    that did not exist on a db written before the change and the watcher logged
    "could not read the bridge store" on every tick — an unreadable store, which
    this module correctly refuses to report as "no bridges in flight", but for a
    reason that was entirely our own.

    Migrating on read is safe in a way that CREATING on read is not: the file
    already exists, so nothing new appears in a data home that had none. The
    `isfile` guard above is what preserves the status-SSOT rule; this only runs
    after it.
    """
    try:
        _init(path)
    except Exception:
        # A failed migration must not turn a readable store into an unreadable
        # one — the SELECT below will fail loudly on its own if the column is
        # genuinely absent.
        logger.warning("bridge store: schema migration failed on %s", path,
                       exc_info=True)


#: Columns every consumer of an open row needs. `recipient` and `balance_before`
#: are what make a re-measurement possible at all: without them the watcher could
#: only re-read the provider's status, which is the third-party claim this whole
#: module exists not to trust. ⚠️ `origin_chain_id` rides along because `tx_ref`
#: is the ORIGIN transaction: without it an owner seat has no way to know which
#: chain's explorer the hash belongs to, and linking it to the DESTINATION puts a
#: Solana base58 signature inside an EVM explorer URL.
_OPEN_COLUMNS = ("id, user_id, request_id, origin_chain_id, dest_chain_id, "
                 "recipient, currency_out, "
                 "amount_in_raw, min_out_raw, amount_usd, state, tx_ref, "
                 "balance_before, created_at, escalated_at, detail")


def open_bridges_all(*, db_path: Optional[str] = None) -> list:
    """Unresolved rows across EVERY tenant, for the reconciliation ticker.

    Tenant-agnostic ON PURPOSE and used only by the watcher, which settles rows
    and notifies each row's OWN `user_id`. Nothing here crosses a tenant boundary;
    the alternative — a per-tenant sweep — would need a tenant list this module
    has no business holding.
    """
    import os

    from core.sqlite_util import execute_retry
    path = db_path or bridges_db_path()
    if not os.path.isfile(path):
        return []
    _migrate_existing(path)
    rows = execute_retry(path, (
        f"SELECT {_OPEN_COLUMNS} FROM bridges WHERE state IN (?,?) "
        f"ORDER BY created_at ASC"),
        (STATE_PENDING, STATE_IN_FLIGHT), fetch="all") or []
    return [dict(r) if not isinstance(r, dict) else r for r in rows]


def conflicting_open_bridge(*, dest_chain_id: int, recipient: str,
                            currency_out: Optional[str],
                            db_path: Optional[str] = None) -> Optional[dict]:
    """An unresolved bridge to the SAME (destination chain, recipient, asset),
    or None (CR-L12).

    Phase 2 proves an arrival by a balance RISE, which cannot say whose rise it
    is: two open bridges into one balance would each claim the other's funds.
    So at most one may be open per (chain, recipient, asset). An unreadable
    store RAISES — "no bridge in flight" is not an answer we can give about a
    store we could not read.
    """
    want_chain = int(dest_chain_id)
    want_asset = str(currency_out or _NATIVE_EVM).lower()
    for row in open_bridges_all(db_path=db_path):
        try:
            same_chain = int(row.get("dest_chain_id") or 0) == want_chain
        except (TypeError, ValueError):
            same_chain = False
        if (same_chain and _same(row.get("recipient"), recipient)
                and str(row.get("currency_out") or _NATIVE_EVM).lower() == want_asset):
            return row
    return None


#: How long a recorded bridge can still claim an inbound transfer as its own.
_ARRIVAL_CLAIM_WINDOW_SEC = 48 * 3600
#: An unmeasured bridge claims an amount in [floor, floor * this]. The floor is
#: the quote's guaranteed minimum, a few percent under the expected output, so
#: the band covers a normal fill without reaching a stranger's unrelated payment.
_OPEN_BRIDGE_BAND = 1.10


def own_bridge_arrival(*, dest_chain_id: int, recipient: str,
                       currency: Optional[str], amount_raw: int,
                       db_path: Optional[str] = None,
                       now: Optional[float] = None) -> Optional[str]:
    """The id of OUR recorded bridge that this inbound transfer is the arrival
    of, or None.

    The settlement watcher asks this about a transfer that matches no invoice:
    a bridge's destination leg is sent by the provider's solver, an address that
    is neither ours nor a swap router, so without this every bridge home was
    reported to the owner as an unmatched payment (prod 2026-10-04 14:57).

    Exact evidence only: same destination chain, recipient and asset, and either
    the EXACT balance rise phase 2 measured (arrived) or, for a bridge not yet
    measured (pending/in_flight), an amount inside a tight band above its
    floor. Read-only; a missing store — or an unreadable one — confirms
    nothing, so the caller falls back to notifying the owner.
    """
    import os

    from core.sqlite_util import execute_retry
    path = db_path or bridges_db_path()
    if not os.path.isfile(path):
        return None
    since = (now if now is not None else time.time()) - _ARRIVAL_CLAIM_WINDOW_SEC
    try:
        rows = execute_retry(path, (
            "SELECT id, recipient, currency_out, min_out_raw, state, "
            "balance_before, balance_after FROM bridges "
            "WHERE dest_chain_id=? AND state IN (?,?,?) AND created_at>=? "
            "ORDER BY created_at DESC"),
            (int(dest_chain_id), STATE_ARRIVED, STATE_PENDING, STATE_IN_FLIGHT,
             since), fetch="all") or []
    except Exception:
        logger.warning("bridge store unreadable for arrival correlation",
                       exc_info=True)
        return None
    want_asset = str(currency or _NATIVE_EVM).lower()
    amount = int(amount_raw)
    for r in rows:
        row = dict(r) if not isinstance(r, dict) else r
        if not _same(row.get("recipient"), recipient):
            continue
        if str(row.get("currency_out") or _NATIVE_EVM).lower() != want_asset:
            continue
        try:
            if row.get("state") == STATE_ARRIVED:
                before, after = row.get("balance_before"), row.get("balance_after")
                if before is not None and after is not None \
                        and int(after) - int(before) == amount:
                    return row["id"]
            else:
                floor = int(row.get("min_out_raw") or 0)
                if floor > 0 and floor <= amount <= int(floor * _OPEN_BRIDGE_BAND):
                    return row["id"]
        except (TypeError, ValueError):
            continue
    return None


def mark_escalated(bid: str, *, at: Optional[float] = None,
                   db_path: Optional[str] = None) -> None:
    """Stamp that the owner has been told. Fail-open."""
    from core.sqlite_util import execute_retry
    try:
        execute_retry(db_path or bridges_db_path(),
                      "UPDATE bridges SET escalated_at=? WHERE id=?",
                      (at if at is not None else time.time(), bid))
    except Exception:
        logger.warning("bridge %s: could not stamp escalation", bid, exc_info=True)


def open_bridges(user_id: str, *, db_path: Optional[str] = None) -> list:
    """Rows that have not reached a terminal state — what the owner is owed an
    answer about. Empty list on any read failure is NOT used: an unreadable
    store raises, because silently reporting 'no bridges in flight' over a real
    one is the confident-and-wrong failure this codebase keeps paying for."""
    import os

    from core.sqlite_util import execute_retry
    path = db_path or bridges_db_path()
    # A READ must never CREATE the store (the status-SSOT rule). No file means no
    # bridge was ever recorded, which is a real answer; `_init`-ing here would
    # leave an empty db in whatever data home happened to resolve.
    if not os.path.isfile(path):
        return []
    _migrate_existing(path)
    rows = execute_retry(path, (
        f"SELECT {_OPEN_COLUMNS} FROM bridges WHERE user_id=? AND state IN (?,?) "
        f"ORDER BY created_at DESC"),
        (str(user_id), STATE_PENDING, STATE_IN_FLIGHT), fetch="all") or []
    return [dict(r) if not isinstance(r, dict) else r for r in rows]


def token_balance_raw(address: str, chain_name: str, token: str) -> Optional[int]:
    """ERC-20 balance in RAW units, or None for UNKNOWN.

    The twin of :func:`native_balance_raw`, and None is load-bearing for the same
    reason: a dead RPC returning 0 is indistinguishable from funds that never
    arrived, and only one of those is an incident.
    """
    from core.wallet.onchain import token_balances
    try:
        got = token_balances(address, chain_name, [token]) or {}
        value = got.get(token)
        if value is None:
            # `token_balances` may key by a differently-cased address; one
            # case-folded retry before calling it unknown.
            low = {str(k).lower(): v for k, v in got.items()}
            value = low.get(str(token).lower())
        return None if value is None else int(value)
    except Exception:
        logger.debug("bridge: token balance read failed on %s", chain_name,
                     exc_info=True)
        return None


def arrival_reader(currency_out: Optional[str],
                   chain_name: Optional[str] = None) -> Callable:
    """The balance reader that matches what is ARRIVING, and WHERE.

    Phase 2's whole claim is "we measured it land". Measuring the NATIVE balance
    for an ERC-20 arrival would measure a number that cannot move and report a
    real delivery as `in_flight` forever — so the asset decides the reader, and
    the choice is made from the quote's own `currency_out`, not from a flag.
    An SVM destination reads lamports or a classic-SPL balance over the Solana
    RPC; ``eth_getBalance`` there would read nothing at all.
    """
    from core.wallet import chains as _chains
    row = _chains.get(chain_name or "") if chain_name else None
    if row is not None and getattr(row, "family", "evm") == "svm":
        from core.wallet import solana_onchain
        if not currency_out or str(currency_out) == _NATIVE_SVM:
            return lambda address, _chain: solana_onchain.native_balance_raw(address)
        mint = str(currency_out)
        return lambda address, _chain: solana_onchain.token_balance_raw(address, mint)
    if not currency_out or str(currency_out).lower() == _NATIVE_EVM:
        return native_balance_raw

    def _read(address: str, chain_name: str) -> Optional[int]:
        return token_balance_raw(address, chain_name, str(currency_out))
    return _read


def native_balance_raw(address: str, chain_name: str) -> Optional[int]:
    """Native balance in RAW units (wei), or None for UNKNOWN.

    None is load-bearing. A dead RPC returning 0 is indistinguishable from funds
    that never arrived, and the caller must be able to tell those apart.
    """
    from core.wallet.onchain import _hex_int, _rpc, rpc_url_for_chain
    try:
        url = rpc_url_for_chain(chain_name)
        if not url:
            return None
        return _hex_int(_rpc(url, "eth_getBalance", [address, "latest"], 6.0))
    except Exception:
        logger.debug("bridge: native balance read failed on %s", chain_name, exc_info=True)
        return None


def evm_tx_inflow(tx_hash: str, recipient: str, currency_out: Optional[str],
                  chain_name: str, *, rpc: Optional[Callable] = None
                  ) -> Optional[tuple]:
    """What ONE destination transaction paid ``recipient`` on an EVM chain, and
    when: ``(raw inflow, block timestamp)``, or None when it cannot be read or
    did not succeed.

    An ERC-20 arrival is the sum of the receipt's ``Transfer`` logs from the
    pinned token contract TO the recipient. A native arrival is the tx ``value``
    when the tx is sent straight to the recipient (the way a solver fills), else
    the recipient's balance change across that one block.
    """
    from core.wallet.onchain import _hex_int, _rpc, rpc_url_for_chain
    try:
        url = rpc_url_for_chain(chain_name)
        if not url:
            return None

        def call(method, params):
            return (rpc(method, params) if rpc is not None
                    else _rpc(url, method, params, 6.0))
        receipt = call("eth_getTransactionReceipt", [tx_hash])
        if not isinstance(receipt, dict) or _hex_int(receipt.get("status")) != 1:
            return None
        block_no = receipt.get("blockNumber")
        block = call("eth_getBlockByNumber", [block_no, False])
        block_time = _hex_int((block or {}).get("timestamp"))
        if block_time is None:
            return None
        want = str(recipient or "").lower()
        if not currency_out or str(currency_out).lower() == _NATIVE_EVM:
            tx = call("eth_getTransactionByHash", [tx_hash])
            if isinstance(tx, dict) and _same(tx.get("to"), recipient):
                value = _hex_int(tx.get("value"))
                return None if value is None else (int(value), int(block_time))
            n = _hex_int(block_no)
            if n is None or n <= 0:
                return None
            after = _hex_int(call("eth_getBalance", [recipient, hex(n)]))
            before = _hex_int(call("eth_getBalance", [recipient, hex(n - 1)]))
            if after is None or before is None:
                return None
            return int(after) - int(before), int(block_time)
        token = str(currency_out).lower()
        total = 0
        for log in receipt.get("logs") or []:
            topics = log.get("topics") or []
            if (str(log.get("address") or "").lower() != token or len(topics) != 3
                    or str(topics[0]).lower() != _TRANSFER_TOPIC
                    or str(topics[2]).lower()[-40:] != want[-40:]):
                continue
            amount = _hex_int(log.get("data"))
            if amount is None:
                return None
            total += int(amount)
        return total, int(block_time)
    except Exception:
        logger.debug("bridge: destination tx read failed on %s", chain_name,
                     exc_info=True)
        return None


class ArrivalProof:
    """``() -> raw inflow | None``: what the DELIVERING transactions Relay names
    paid ``recipient``, bound to THIS bridge (B-1).

    Relay's destination hashes are only pointers. Each one is read on-chain and
    counts only when it is:
      * new in this list (``[h, h]`` is one transaction, not two);
      * not older than the bridge (block time >= ``created_at`` minus
        :data:`PROOF_SKEW_SEC`) — an earlier delivery is not this one;
      * not already claimed by ANOTHER bridge row (``bridge_proofs``).
    When the counted sum clears ``min_out_raw`` the hashes are claimed for this
    row in one transaction; a lost race reads as unproven (None).

    ``requires_delta``: on an EVM chain the measured balance rise is kept as a
    second check (B-2). On Solana it is not — the lab trades on the same
    wallet, so a balance rise there proves nothing.

    ``basis`` after a call: ``"tx"`` when hashes were read, or
    :data:`BASIS_BALANCE_ONLY` when Relay answered with NO destination hash on
    an EVM chain. Then the caller may accept the balance rise from the fresh
    in-reserve baseline alone (so a real delivery is not left open forever);
    on Solana an empty list never falls back.
    """

    def __init__(self, *, request_id: str, recipient: str,
                 currency_out: Optional[str], chain_name: str, svm: bool,
                 bridge_id: Optional[str] = None, dest_chain_id=None,
                 created_at: Optional[float] = None,
                 min_out_raw: Optional[int] = None, db_path: Optional[str] = None):
        self.request_id = request_id
        self.recipient = recipient
        self.currency_out = currency_out
        self.chain_name = chain_name
        self.svm = svm
        self.requires_delta = not svm
        self.bridge_id = bridge_id
        self.dest_chain_id = dest_chain_id
        self.created_at = created_at
        self.min_out_raw = min_out_raw
        self.db_path = db_path
        self.basis: Optional[str] = None

    def _measure(self, tx_hash: str) -> Optional[tuple]:
        if self.svm:
            from core.wallet import solana_onchain
            mint = (None if not self.currency_out or str(self.currency_out) == _NATIVE_SVM
                    else str(self.currency_out))
            return solana_onchain.tx_inflow_at(tx_hash, self.recipient, mint)
        return evm_tx_inflow(tx_hash, self.recipient, self.currency_out,
                             self.chain_name)

    def __call__(self) -> Optional[int]:
        from core.wallet import bridge_status
        self.basis = None
        hashes = bridge_status.read_out_txs(self.request_id)
        if hashes is not None and not hashes and not self.svm:
            self.basis = BASIS_BALANCE_ONLY
            return None
        if not hashes:
            return None
        self.basis = "tx"
        seen, unique = set(), []
        for h in hashes:
            key = _hash_key(h)
            if key and key not in seen:
                seen.add(key)
                unique.append(h)
        total, used = 0, []
        for h in unique:
            if self.bridge_id and proof_claimed_by_other(
                    self.bridge_id, self.dest_chain_id, h, db_path=self.db_path):
                logger.warning("bridge %s: destination tx %s is already another "
                               "bridge's proof; not counted", self.bridge_id, h)
                continue
            got = self._measure(h)
            if got is None:
                return None
            amount, block_time = got
            if self.created_at is not None and (
                    block_time is None
                    or float(block_time) < float(self.created_at) - PROOF_SKEW_SEC):
                logger.warning("bridge %s: destination tx %s predates the bridge; "
                               "not counted", self.bridge_id, h)
                continue
            if int(amount) <= 0:
                continue
            total += int(amount)
            used.append(h)
        if (self.bridge_id and self.min_out_raw is not None
                and total >= int(self.min_out_raw)):
            if not claim_proof(self.bridge_id, self.dest_chain_id, used,
                               db_path=self.db_path):
                return None
        return total


def arrival_prover(request_id: str, recipient: str, currency_out: Optional[str],
                   chain_name: Optional[str], *, bridge_id: Optional[str] = None,
                   dest_chain_id=None, created_at: Optional[float] = None,
                   min_out_raw: Optional[int] = None,
                   db_path: Optional[str] = None) -> Optional[ArrivalProof]:
    """The :class:`ArrivalProof` for a registry destination chain, or None when
    the chain is not in the registry. Every family proves an arrival from the
    delivering transaction (SVM since 037, EVM since 085 B-2)."""
    from core.wallet import chains as _chains
    row = _chains.get(chain_name or "") if chain_name else None
    if row is None:
        return None
    return ArrivalProof(
        request_id=request_id, recipient=recipient, currency_out=currency_out,
        chain_name=str(chain_name), svm=getattr(row, "family", "evm") == "svm",
        bridge_id=bridge_id, dest_chain_id=dest_chain_id, created_at=created_at,
        min_out_raw=min_out_raw, db_path=db_path)


@dataclass(frozen=True)
class ArrivalOutcome:
    state: str            # arrived | in_flight | failed
    detail: str
    balance_after: Optional[int] = None
    measured_delta: Optional[int] = None


def await_arrival(*, provider, request_id: str, recipient: str, chain_name: str,
                  min_out_raw: int, balance_before: Optional[int],
                  deadline_sec: int = DEFAULT_ARRIVAL_DEADLINE_SEC,
                  poll_sec: float = ARRIVAL_POLL_SEC,
                  now: Callable[[], float] = time.time,
                  sleep: Callable[[float], None] = time.sleep,
                  read_balance: Optional[Callable[[str, str], Optional[int]]] = None,
                  prove: Optional[Callable[[], Optional[int]]] = None
                  ) -> ArrivalOutcome:
    """Phase 2. Poll until the destination balance clears the floor, or give up
    honestly.

    The MEASURED balance is the assertion. The provider's status only decides
    whether to keep waiting (`pending`) or stop early (`failure`/refund); a
    provider that says `success` while the balance has not moved is not believed
    — that combination returns `in_flight`, because one of the two is wrong and
    we do not get to pick which.
    """
    reader = read_balance or native_balance_raw
    started = now()
    last_detail = "no status read yet"
    provider_failed = False

    needs_delta = bool(getattr(prove, "requires_delta", False))
    while True:
        after = reader(recipient, chain_name)
        delta_now = (None if (after is None or balance_before is None)
                     else int(after) - int(balance_before))
        if prove is None and delta_now is not None and delta_now >= int(min_out_raw):
            return ArrivalOutcome(
                STATE_ARRIVED,
                f"measured +{delta_now} raw on {chain_name} (floor {min_out_raw})",
                balance_after=int(after), measured_delta=delta_now)

        state, detail = provider.status(request_id)
        last_detail = detail or state
        delta_ok = (not needs_delta) or (
            delta_now is not None and delta_now >= int(min_out_raw))
        if prove is not None and state == "success" and delta_ok:
            # The delivering transaction's own transfer is the proof (B-1/B-2).
            # On EVM the measured balance rise must ALSO clear the floor (the
            # second check); on Solana it is not asked (the lab trades here).
            proven = prove()
            if proven is not None and proven >= int(min_out_raw):
                return ArrivalOutcome(
                    STATE_ARRIVED,
                    f"proven +{proven} raw by the delivering transaction on "
                    f"{chain_name} (floor {min_out_raw})",
                    balance_after=after, measured_delta=proven)
            if (proven is None and getattr(prove, "basis", None) == BASIS_BALANCE_ONLY
                    and delta_now is not None and delta_now >= int(min_out_raw)):
                # Relay named no destination tx: the rise over the baseline
                # read inside the reservation is the proof (recorded as such).
                return ArrivalOutcome(
                    STATE_ARRIVED,
                    f"measured +{delta_now} raw on {chain_name} (floor "
                    f"{min_out_raw}); proof basis: balance-only (relay returned "
                    f"no destination tx)",
                    balance_after=int(after), measured_delta=delta_now)
        if state == "failure":
            provider_failed = True
            break
        if now() - started >= deadline_sec:
            break
        sleep(poll_sec)

    after = reader(recipient, chain_name)
    delta = (None if (after is None or balance_before is None)
             else int(after) - int(balance_before))

    if provider_failed:
        # A provider-declared failure with NO measured inflow is the one case we
        # can call failed: the order was refunded or expired and the money did
        # not move to the destination.
        if delta is None or delta < int(min_out_raw):
            return ArrivalOutcome(
                STATE_FAILED,
                f"relay reports failure/refund ({last_detail}); measured delta "
                f"{'unknown' if delta is None else delta} on {chain_name}",
                balance_after=after, measured_delta=delta)

    return ArrivalOutcome(
        STATE_IN_FLIGHT,
        (f"not confirmed within {deadline_sec}s. relay says: {last_detail}. "
         f"measured delta on {chain_name}: "
         f"{'UNKNOWN (balance unreadable)' if delta is None else delta} "
         f"(floor {min_out_raw}). This is NOT a failure and NOT a success — do "
         f"NOT re-send; a re-sent bridge pays twice. Check "
         f"`polyrob wallet bridges` or the relay request id."),
        balance_after=after, measured_delta=delta)


def _same(a, b) -> bool:
    """Case-insensitive for 0x-hex only; base58 is case-SENSITIVE
    (``core.wallet.addresses.same_address``)."""
    from core.wallet.addresses import same_address
    return same_address(a, b)
