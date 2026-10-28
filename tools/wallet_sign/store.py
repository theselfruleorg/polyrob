"""``<data_home>/wallet/wallet_sign.db`` — what the wallet signed, and what it got back.

Two tables:

* ``signatures`` — one row per signature: when, scheme, site, class, the digest,
  whether the turn was autonomous, and the sign-in nonce. It is the replay log
  (a ``(domain, nonce)`` pair signs once) and the autonomous rate count (090 D2).
  The signature itself is never stored.
* ``credentials`` — a credential a site returned for a signature (402Mob's
  merchant key), Fernet-encrypted (``core.security.encryption``) under
  ``(origin, wallet, label)``. Only its fingerprint is ever shown.

An unreadable store is not an empty one: every read raises, and the tool
refuses rather than sign without its replay check.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signatures (
    ts REAL NOT NULL,
    scheme TEXT NOT NULL,
    origin TEXT NOT NULL,
    kind TEXT NOT NULL,
    cls TEXT NOT NULL,
    digest TEXT NOT NULL,
    address TEXT NOT NULL,
    autonomous INTEGER NOT NULL,
    nonce TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS signatures_nonce ON signatures(origin, nonce)
    WHERE nonce IS NOT NULL;
CREATE INDEX IF NOT EXISTS signatures_origin_ts ON signatures(origin, ts);
CREATE TABLE IF NOT EXISTS credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origin TEXT NOT NULL,
    wallet TEXT NOT NULL,
    label TEXT NOT NULL,
    secret BLOB NOT NULL,
    fingerprint TEXT NOT NULL,
    created REAL NOT NULL,
    UNIQUE(origin, wallet, label)
);
"""


#: Registered in ``core.db_manifest`` (home ``wallet``).
DB_NAME = "wallet_sign.db"


def store_path(data_home: Optional[str] = None) -> str:
    """``<data_home>/wallet/<DB_NAME>`` — beside the pins and the audit."""
    if data_home:
        return os.path.join(str(data_home), "wallet", DB_NAME)
    from core.wallet.audit_sink import _wallet_data_dir
    return os.path.join(_wallet_data_dir(), DB_NAME)


class WalletSignStore:
    def __init__(self, path: Optional[str] = None):
        self.path = path or store_path()
        from core.sqlite_util import init_schema
        init_schema(self.path, _SCHEMA, mkdir=True)

    def _db(self):
        from core.sqlite_util import wal_connect
        return wal_connect(self.path)

    # -- signatures ------------------------------------------------------------

    def reserve(self, *, scheme: str, origin: str, kind: str, cls: str, digest: str,
                address: str, autonomous: bool, nonce: Optional[str], now: float,
                per_origin_day: int, all_origins_day: int) -> int:
        """Write the signature row BEFORE signing, in one write transaction with
        the replay and rate checks, so two concurrent calls cannot both pass.
        Returns the row id; raises ``ValueError`` naming the refusal."""
        db = self._db()
        try:
            db.execute("BEGIN IMMEDIATE")
            if nonce is not None and db.execute(
                    "SELECT 1 FROM signatures WHERE origin=? AND nonce=?",
                    (origin, nonce)).fetchone():
                raise ValueError(f"this sign-in nonce for {origin} was signed already — a "
                                 f"replayed login request is never signed twice")
            if autonomous:
                since = now - 86400
                used = db.execute("SELECT COUNT(*) FROM signatures WHERE origin=? AND "
                                  "autonomous=1 AND ts>=?", (origin, since)).fetchone()[0]
                if used >= per_origin_day:
                    raise ValueError(f"autonomous runs already signed {used} messages for "
                                     f"{origin} in 24 h (limit {per_origin_day})")
                total = db.execute("SELECT COUNT(*) FROM signatures WHERE autonomous=1 AND "
                                   "ts>=?", (since,)).fetchone()[0]
                if total >= all_origins_day:
                    raise ValueError(f"autonomous runs already signed {total} messages in 24 h "
                                     f"(limit {all_origins_day} across all sites)")
            cur = db.execute("INSERT INTO signatures VALUES (?,?,?,?,?,?,?,?,?)",
                             (now, scheme, origin, kind, cls, digest, address,
                              1 if autonomous else 0, nonce))
            db.commit()
            return int(cur.lastrowid)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def release(self, row_id: int) -> None:
        """Drop a reservation whose signature was never produced."""
        db = self._db()
        try:
            db.execute("DELETE FROM signatures WHERE rowid=?", (int(row_id),))
            db.commit()
        finally:
            db.close()

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        db = self._db()
        try:
            rows = db.execute("SELECT ts, scheme, origin, kind, cls, digest, autonomous "
                              "FROM signatures ORDER BY ts DESC LIMIT ?", (int(limit),)).fetchall()
        finally:
            db.close()
        keys = ("ts", "scheme", "origin", "kind", "cls", "digest", "autonomous")
        return [dict(zip(keys, r)) for r in rows]

    # -- credentials -----------------------------------------------------------

    def save_credential(self, *, origin: str, wallet: str, label: str, secret: str) -> str:
        """Encrypt and keep *secret*; the fingerprint is the only thing shown."""
        from core.security.encryption import get_encryption
        from core.security.redaction import fingerprint
        fp = fingerprint(secret)
        blob = get_encryption().encrypt(secret)
        db = self._db()
        try:
            db.execute("INSERT INTO credentials (origin, wallet, label, secret, fingerprint, "
                       "created) VALUES (?,?,?,?,?,?) ON CONFLICT(origin, wallet, label) DO "
                       "UPDATE SET secret=excluded.secret, fingerprint=excluded.fingerprint, "
                       "created=excluded.created",
                       (origin, wallet, label, blob, fp, time.time()))
            db.commit()
        finally:
            db.close()
        return fp

    def credentials(self) -> List[Dict[str, Any]]:
        db = self._db()
        try:
            rows = db.execute("SELECT id, origin, wallet, label, fingerprint, created "
                              "FROM credentials ORDER BY created DESC").fetchall()
        finally:
            db.close()
        keys = ("id", "origin", "wallet", "label", "fingerprint", "created")
        return [dict(zip(keys, r)) for r in rows]

    def credential_secret(self, credential_id: int) -> Optional[Dict[str, Any]]:
        """``{origin, label, secret}`` decrypted, for the ONE in-process use
        (``wallet_sign_api_call``); never returned from a tool."""
        db = self._db()
        try:
            row = db.execute("SELECT origin, label, secret FROM credentials WHERE id=?",
                             (int(credential_id),)).fetchone()
        finally:
            db.close()
        if row is None:
            return None
        from core.security.encryption import get_encryption
        return {"origin": row[0], "label": row[1], "secret": get_encryption().decrypt(row[2])}
