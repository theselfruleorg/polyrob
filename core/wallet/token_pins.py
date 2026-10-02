"""Owner-pinned token identities (068 G1).

The canonical list (``tokens.CANONICAL_TOKENS``) pins only USDC and the wrapped
native per chain. Everything the instance itself trades by NAME — its own
launch, a buyback target — was an unverified address the model carried in
prose, and on 2026-09-25 a buyback cron whose text had lost the address bought
an airdropped look-alike ("PNL", "Pissin N Lying") for $134.54 instead of the
track-record token.

A pin binds ``(chain, SYMBOL) -> address`` on the OWNER's word. Two effects:

* ``tokens.get_token_identity`` reports a pinned address as ``verified=True``
  (``source="owner_pin"``) — the owner checked it, which is what verified means;
* ``tools/defi/identity_gate`` refuses to BUY any other contract that claims a
  pinned symbol on that chain.

⚠️ The writers are OWNER seats only, never an agent verb: the owner CLI
(``polyrob wallet pin-token``, ``source="owner_pin"``) and ``core.wallet.
token_trust`` — the owner's ``/wallet trust`` and the Approve tap on a
``token_identity`` ask (``source="owner_approved"``). A pin the agent could
write is a verification the agent could grant itself, which is the exact hole
this closes. ``token_trust`` refuses any caller that is not a genuine owner turn.

W1: the same store holds the owner's NOT-trusted verdicts (``token_rejections``):
an address the owner said is not the real token. The identity gate refuses a buy
of it and never asks about it again. Trusting an address clears its rejection
and rejecting it removes its pin — one address, one verdict.

A read failure returns "no pins" — the gate then treats the token as unverified,
which is the fail-closed direction (it refuses more, never less).
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from core import sqlite_util

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS token_pins (
    chain     TEXT NOT NULL,
    address   TEXT NOT NULL,
    symbol    TEXT NOT NULL,
    note      TEXT NOT NULL DEFAULT '',
    pinned_ts REAL NOT NULL,
    source    TEXT NOT NULL DEFAULT 'owner_pin',
    PRIMARY KEY (chain, address)
)
"""

_REJECTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS token_rejections (
    chain       TEXT NOT NULL,
    address     TEXT NOT NULL,
    symbol      TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    rejected_ts REAL NOT NULL,
    PRIMARY KEY (chain, address)
)
"""

#: Who vouched for a pin. ``owner_pin``: the owner CLI. ``owner_approved``: the
#: owner tapped Approve on a token-identity ask, or ran ``/wallet trust``.
SOURCE_OWNER_PIN = "owner_pin"
SOURCE_OWNER_APPROVED = "owner_approved"
PIN_SOURCES = (SOURCE_OWNER_PIN, SOURCE_OWNER_APPROVED)


def token_pins_db_path(data_home: Optional[str] = None) -> str:
    """``<data_home>/wallet/token_pins.db`` — beside the spend audit."""
    if data_home:
        return os.path.join(str(data_home), "wallet", "token_pins.db")
    from core.wallet.audit_sink import _wallet_data_dir
    return os.path.join(_wallet_data_dir(), "token_pins.db")


def _norm_symbol(symbol: Optional[str]) -> str:
    return str(symbol or "").strip().upper()


class PinStoreUnreadable(RuntimeError):
    """The pin store EXISTS but could not be read (permissions, corruption).

    Not the same fact as "there are no pins" (068 B1): a spend path that reads
    an unreadable store as empty drops the owner's word about which contract is
    real, which is the permissive direction. The strict readers raise this; the
    read-side views say UNREADABLE.
    """


def norm_chain(chain: Optional[str]) -> str:
    """ONE chain-key rule for pins and the gates (068 B2): stripped, lowercase —
    the same fold ``chains.get`` applies, so `BASE` and `base` are one chain."""
    return str(chain or "").strip().lower()


def _norm_address(chain: str, address: str) -> str:
    """The chain's canonical address form (068 B3): EVM checksummed, Solana
    base58 byte-for-byte (case-SENSITIVE). An unknown chain keeps the old
    shape-based rule rather than refusing an owner write outright."""
    address = str(address or "").strip()
    if not address:
        raise ValueError("empty address")
    try:
        from core.wallet.addresses import normalize_for_chain
        return normalize_for_chain(norm_chain(chain), address)
    except ValueError:
        from core.wallet import chains
        if chains.get(norm_chain(chain)) is not None:
            raise
    if address.startswith("0x") or address.startswith("0X"):
        from core.wallet.tokens import normalize_address
        return normalize_address(address)
    return address


def _same(a: str, b: str) -> bool:
    from core.wallet.addresses import same_address
    return same_address(a, b)


def store_absent(path: str) -> bool:
    """True ONLY when *path* genuinely does not exist.

    068 B1 (round 2): ``os.path.isfile`` answers False for a file it cannot
    reach — a parent directory without search permission reads exactly like "no
    file", and "no file" means "no pins" — so an unreadable store silently lifted
    every pin. Only ``FileNotFoundError`` is absence; any other ``OSError``
    (``PermissionError`` on the file or a parent, ``NotADirectoryError``…)
    raises, and the caller treats the store as UNREADABLE.
    """
    try:
        os.stat(path)
    except FileNotFoundError:
        return True
    return False


def _columns(conn) -> set:
    return {r[1] for r in conn.execute("PRAGMA table_info(token_pins)").fetchall()}


def _connect(path: str, *, create: bool):
    if not create and store_absent(path):
        return None
    if create:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite_util.wal_connect(path)
    conn.execute(_SCHEMA)
    if create:
        # W1: migration-safe — a pre-W1 store gains the source column on its
        # first WRITE (a reader never alters; it selects a default instead).
        if "source" not in _columns(conn):
            conn.execute("ALTER TABLE token_pins ADD COLUMN source TEXT NOT NULL "
                         "DEFAULT 'owner_pin'")
        conn.execute(_REJECTIONS_SCHEMA)
    return conn


def pin(chain: str, address: str, symbol: str, *, note: str = "",
        source: str = SOURCE_OWNER_PIN,
        db_path: Optional[str] = None) -> Dict[str, str]:
    """Pin *address* as THE *symbol* on *chain*. Owner seats only.

    One symbol names one contract per chain: pinning a second address for the
    same symbol replaces the first (the owner is correcting it), and the old
    row is returned in ``replaced`` so the CLI can say so.
    """
    chain = norm_chain(chain)
    sym = _norm_symbol(symbol)
    if not chain or not sym:
        raise ValueError("chain and symbol are required")
    if source not in PIN_SOURCES:
        raise ValueError(f"unknown pin source {source!r}; one of {PIN_SOURCES}")
    addr = _norm_address(chain, address)
    path = db_path or token_pins_db_path()
    conn = _connect(path, create=True)
    try:
        _drop_rejection(conn, chain, addr)
        rows = conn.execute("SELECT address, symbol FROM token_pins WHERE chain=?",
                            (chain,)).fetchall()
        prior = [r[0] for r in rows if r[1] == sym and not _same(r[0], addr)]
        # The same contract under another symbol, or this symbol on another
        # contract: both are replaced — one symbol, one contract, per chain.
        for r in rows:
            if r[1] == sym or _same(r[0], addr):
                conn.execute("DELETE FROM token_pins WHERE chain=? AND address=?",
                             (chain, r[0]))
        conn.execute(
            "INSERT OR REPLACE INTO token_pins (chain, address, symbol, note, "
            "pinned_ts, source) VALUES (?, ?, ?, ?, ?, ?)",
            (chain, addr, sym, str(note or ""), time.time(), source))
        conn.commit()
    finally:
        conn.close()
    return {"chain": chain, "address": addr, "symbol": sym, "source": source,
            "replaced": ", ".join(prior)}


def _drop_rejection(conn, chain: str, addr: str) -> None:
    rows = conn.execute("SELECT address FROM token_rejections WHERE chain=?",
                        (chain,)).fetchall()
    for r in rows:
        if _same(r[0], addr):
            conn.execute("DELETE FROM token_rejections WHERE chain=? AND address=?",
                         (chain, r[0]))


def reject(chain: str, address: str, *, symbol: str = "", note: str = "",
           db_path: Optional[str] = None) -> Dict[str, str]:
    """Record the owner's word that *address* is NOT the token (W1). Owner
    seats only. Removes any pin on the same address."""
    chain = norm_chain(chain)
    if not chain:
        raise ValueError("chain is required")
    addr = _norm_address(chain, address)
    conn = _connect(db_path or token_pins_db_path(), create=True)
    try:
        _drop_rejection(conn, chain, addr)
        for r in conn.execute("SELECT address FROM token_pins WHERE chain=?",
                              (chain,)).fetchall():
            if _same(r[0], addr):
                conn.execute("DELETE FROM token_pins WHERE chain=? AND address=?",
                             (chain, r[0]))
        conn.execute(
            "INSERT INTO token_rejections (chain, address, symbol, note, rejected_ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (chain, addr, _norm_symbol(symbol), str(note or "")[:300], time.time()))
        conn.commit()
    finally:
        conn.close()
    return {"chain": chain, "address": addr, "symbol": _norm_symbol(symbol)}


def unreject(chain: str, address: str, *, db_path: Optional[str] = None) -> bool:
    """Lift a NOT-trusted verdict. True when one was removed."""
    chain = norm_chain(chain)
    addr = _norm_address(chain, address)
    path = db_path or token_pins_db_path()
    if store_absent(path):
        return False
    conn = _connect(path, create=True)
    try:
        before = conn.total_changes
        _drop_rejection(conn, chain, addr)
        conn.commit()
        return conn.total_changes > before
    finally:
        conn.close()


def all_rejections(*, chain: Optional[str] = None, db_path: Optional[str] = None,
                   strict: bool = False) -> List[Dict[str, str]]:
    """Every NOT-trusted verdict (optionally for one chain). ``strict`` raises
    ``PinStoreUnreadable`` on a store that exists but cannot be read."""
    path = db_path or token_pins_db_path()
    try:
        conn = _connect(path, create=False)
    except Exception as exc:
        if strict:
            raise PinStoreUnreadable(f"{path}: {type(exc).__name__}: {exc}") from exc
        logger.warning("token rejections unreadable", exc_info=True)
        return []
    if conn is None:
        return []
    try:
        has = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND "
                           "name='token_rejections'").fetchone()
        if not has:
            return []
        if chain:
            rows = conn.execute(
                "SELECT chain, address, symbol, note, rejected_ts FROM token_rejections "
                "WHERE chain=? ORDER BY rejected_ts", (norm_chain(chain),)).fetchall()
        else:
            rows = conn.execute(
                "SELECT chain, address, symbol, note, rejected_ts FROM token_rejections "
                "ORDER BY chain, rejected_ts").fetchall()
    except Exception as exc:
        if strict:
            raise PinStoreUnreadable(f"{path}: {type(exc).__name__}: {exc}") from exc
        logger.warning("token rejections unreadable", exc_info=True)
        return []
    finally:
        conn.close()
    return [{"chain": r[0], "address": r[1], "symbol": r[2], "note": r[3],
             "rejected_ts": r[4]} for r in rows]


def rejection(chain: str, address: str, *, db_path: Optional[str] = None,
              strict: bool = False) -> Optional[Dict[str, str]]:
    """The owner's NOT-trusted verdict for ``(chain, address)``, or None."""
    for row in all_rejections(chain=chain, db_path=db_path, strict=strict):
        if _same(row["address"], address):
            return row
    return None


def unpin(chain: str, address: str, *, db_path: Optional[str] = None) -> bool:
    chain = norm_chain(chain)
    addr = _norm_address(chain, address)
    conn = _connect(db_path or token_pins_db_path(), create=False)
    if conn is None:
        return False
    try:
        rows = conn.execute("SELECT address FROM token_pins WHERE chain=?",
                            (chain,)).fetchall()
        hit = [r[0] for r in rows if _same(r[0], addr)]
        for a in hit:
            conn.execute("DELETE FROM token_pins WHERE chain=? AND address=?", (chain, a))
        conn.commit()
        return bool(hit)
    finally:
        conn.close()


def _read_rows(chain: Optional[str], db_path: Optional[str]):
    """Rows, ``[]`` when the store is ABSENT; raises ``PinStoreUnreadable``
    when it exists and cannot be read."""
    path = db_path or token_pins_db_path()
    try:
        conn = _connect(path, create=False)
    except Exception as exc:
        raise PinStoreUnreadable(f"{path}: {type(exc).__name__}: {exc}") from exc
    if conn is None:
        return []
    try:
        src = "source" if "source" in _columns(conn) else "'owner_pin' AS source"
        if chain:
            rows = conn.execute(
                f"SELECT chain, address, symbol, note, {src} FROM token_pins "
                "WHERE chain=? ORDER BY symbol", (norm_chain(chain),)).fetchall()
        else:
            rows = conn.execute(
                f"SELECT chain, address, symbol, note, {src} FROM token_pins "
                "ORDER BY chain, symbol").fetchall()
    except Exception as exc:
        raise PinStoreUnreadable(f"{path}: {type(exc).__name__}: {exc}") from exc
    finally:
        conn.close()
    return [{"chain": r[0], "address": r[1], "symbol": r[2], "note": r[3],
             "source": r[4] or SOURCE_OWNER_PIN} for r in rows]


def all_pins(*, chain: Optional[str] = None, db_path: Optional[str] = None,
             strict: bool = False) -> List[Dict[str, str]]:
    """Every pin (optionally for one chain).

    ``strict=True`` (the spend path) raises ``PinStoreUnreadable`` on a store
    that exists but cannot be read. Non-strict (read-side views) logs and
    returns ``[]`` — callers that DISPLAY pins use ``pins_status`` instead so
    they can say UNREADABLE rather than "no pins".
    """
    try:
        return _read_rows(chain, db_path)
    except PinStoreUnreadable:
        if strict:
            raise
        logger.warning("token pins unreadable", exc_info=True)
        return []


def pins_status(*, chain: Optional[str] = None,
                db_path: Optional[str] = None):
    """``(state, rows, error)`` — state is ``absent`` | ``ok`` | ``unreadable``."""
    path = db_path or token_pins_db_path()
    try:
        if store_absent(path):
            return "absent", [], None
    except OSError as exc:
        return "unreadable", [], f"{path}: {type(exc).__name__}: {exc}"
    try:
        return "ok", _read_rows(chain, path), None
    except PinStoreUnreadable as exc:
        return "unreadable", [], str(exc)


def owner_pin(chain: str, address: str, *, db_path: Optional[str] = None,
              strict: bool = False) -> Optional[Dict[str, str]]:
    """The pin row for ``(chain, address)``, or None (chain-aware equality)."""
    for row in all_pins(chain=chain, db_path=db_path, strict=strict):
        if _same(row["address"], address):
            return row
    return None


def pinned_addresses_for_symbol(chain: str, symbol: Optional[str], *,
                                db_path: Optional[str] = None,
                                strict: bool = False) -> List[str]:
    """Every address PINNED (canonical or owner) as *symbol* on *chain*."""
    sym = _norm_symbol(symbol)
    chain = norm_chain(chain)
    if not sym:
        return []
    out: List[str] = []
    try:
        from core.wallet.tokens import CANONICAL_TOKENS
        for (c, addr), meta in CANONICAL_TOKENS.items():
            if c == chain and _norm_symbol(meta.get("symbol")) == sym:
                out.append(addr)
    except Exception:
        pass
    for row in all_pins(chain=chain, db_path=db_path, strict=strict):
        if row["symbol"] == sym and not any(_same(row["address"], a) for a in out):
            out.append(row["address"])
    return out
