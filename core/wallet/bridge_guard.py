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

STATE_PENDING = "pending"
STATE_ARRIVED = "arrived"
STATE_IN_FLIGHT = "in_flight"
STATE_FAILED = "failed"

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
"""


def bridges_db_path() -> str:
    """`<data_home>/bridges.db`. Resolved at CALL time, never bound at import
    (`tests/test_home_binding_ratchet.py`)."""
    import os
    from core.runtime_paths import resolve_data_home
    return os.path.join(str(resolve_data_home()), "bridges.db")


def _init(db_path: str) -> None:
    from core.sqlite_util import wal_connect
    conn = wal_connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        # Rows written before the column existed. ALTER is not idempotent, so the
        # duplicate-column error is the expected outcome on every run but the
        # first — and it is the ONLY error swallowed here.
        try:
            conn.execute("ALTER TABLE bridges ADD COLUMN escalated_at REAL")
        except Exception as exc:
            if "duplicate column" not in str(exc).lower():
                raise
        conn.commit()
    finally:
        conn.close()


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
    if int(getattr(row, "chain_id", -1)) != int(quote.dest_chain_id):
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


def record_pending(*, user_id: str, quote, amount_usd: Optional[float],
                   balance_before: Optional[int], db_path: Optional[str] = None) -> str:
    """Write the in-flight row BEFORE broadcast. Returns the bridge id.

    Before, not after: the window between a broadcast and its record is exactly
    when a crash makes an in-flight bridge invisible, and an invisible in-flight
    bridge cannot be recovered by anyone.
    """
    from core.sqlite_util import execute_retry
    path = db_path or bridges_db_path()
    _init(path)
    bid = uuid.uuid4().hex[:16]
    execute_retry(path, (
        "INSERT OR REPLACE INTO bridges (id, user_id, request_id, origin_chain_id, "
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


def arrival_reader(currency_out: Optional[str]) -> Callable:
    """The balance reader that matches what is ARRIVING.

    Phase 2's whole claim is "we measured it land". Measuring the NATIVE balance
    for an ERC-20 arrival would measure a number that cannot move and report a
    real delivery as `in_flight` forever — so the asset decides the reader, and
    the choice is made from the quote's own `currency_out`, not from a flag.
    """
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
                  read_balance: Optional[Callable[[str, str], Optional[int]]] = None
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

    while True:
        after = reader(recipient, chain_name)
        if after is not None and balance_before is not None:
            delta = int(after) - int(balance_before)
            if delta >= int(min_out_raw):
                return ArrivalOutcome(
                    STATE_ARRIVED,
                    f"measured +{delta} raw on {chain_name} (floor {min_out_raw})",
                    balance_after=int(after), measured_delta=delta)

        state, detail = provider.status(request_id)
        last_detail = detail or state
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
