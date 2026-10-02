"""The signer's own state (066 §5.1): nonces, approvals, pause, decisions.

One SQLite file, ``<state_dir>/signer.sqlite``, mode 0600, owned by
``polyrob-signer``. The agent cannot write it (it is not in the signer's group
and the directory is 0700), so it cannot reset the nonce journal, forge an
approval or lift the pause. The spend ledger is the ordinary
``JsonlAuditSink`` under ``<state_dir>/wallet/`` — the same code, a different
directory — so the rolling cap here is the SIGNER's, whatever the agent's own
ledger says.

Tables:

* ``nonces`` — every nonce the signer signed for, per (chain, address). A
  second request for a nonce at or below the highest one used is ``nonce_reuse``.
* ``approvals`` — requests above the hard cap. ``pending`` until uid 0 decides;
  a grant is ONE-SHOT and expires (``approval_ttl_sec``).
* ``flags`` — ``paused`` (the signer's own spend pause).
* ``decisions`` — the last N verdicts (op, digest, allowed, code, reason, peer).

069 v4: a store created before the simple model may still hold an ``account_bindings`` (or
an older ``desk_bindings``) table. It is left in place — never dropped, never read — so no
data is lost; nothing in the signer uses it any more.
"""
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nonces (
  chain TEXT NOT NULL, address TEXT NOT NULL, nonce INTEGER NOT NULL,
  tx_hash TEXT, state TEXT NOT NULL, ts REAL NOT NULL,
  PRIMARY KEY (chain, address, nonce));
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, digest TEXT NOT NULL, op TEXT NOT NULL,
  chain TEXT, amount_usd REAL NOT NULL, summary TEXT NOT NULL,
  state TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
  decided_at REAL, decided_uid INTEGER);
CREATE INDEX IF NOT EXISTS approvals_digest ON approvals(digest);
CREATE TABLE IF NOT EXISTS flags (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
  ts REAL NOT NULL, op TEXT NOT NULL, digest TEXT, allowed INTEGER NOT NULL,
  code TEXT, reason TEXT, peer_uid INTEGER, amount_usd REAL, ref TEXT);
"""
_DECISIONS_KEPT = 5000


class SignerStore:
    def __init__(self, state_dir: str, *, clock=time.time):
        self.state_dir = str(state_dir)
        self.path = os.path.join(self.state_dir, "signer.sqlite")
        self._now = clock
        os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
        if not os.path.exists(self.path):
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        os.chmod(self.path, 0o600)
        with self._db() as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    # -- nonces ------------------------------------------------------------

    def highest_nonce(self, chain: str, address: str) -> Optional[int]:
        with self._db() as db:
            row = db.execute(
                "SELECT MAX(nonce) AS n FROM nonces WHERE chain=? AND address=?",
                (str(chain), str(address).lower())).fetchone()
        return None if row is None or row["n"] is None else int(row["n"])

    def reserve_nonce(self, chain: str, address: str, nonce: int) -> bool:
        """Claim *nonce* before signing. False = it (or a higher one) is used."""
        address = str(address).lower()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT MAX(nonce) AS n FROM nonces WHERE chain=? AND address=?",
                             (str(chain), address)).fetchone()
            if row is not None and row["n"] is not None and int(nonce) <= int(row["n"]):
                db.execute("ROLLBACK")
                return False
            db.execute("INSERT INTO nonces VALUES (?,?,?,?,?,?)",
                       (str(chain), address, int(nonce), None, "reserved", self._now()))
            db.execute("COMMIT")
        return True

    def bind_nonce(self, chain: str, address: str, nonce: int, tx_hash: str) -> None:
        with self._db() as db:
            db.execute("UPDATE nonces SET tx_hash=?, state='sent' WHERE chain=? AND address=? AND nonce=?",
                       (str(tx_hash), str(chain), str(address).lower(), int(nonce)))

    def release_nonce(self, chain: str, address: str, nonce: int) -> None:
        """The node REFUSED the bytes (definitively not sent): the nonce is free."""
        with self._db() as db:
            db.execute("DELETE FROM nonces WHERE chain=? AND address=? AND nonce=? AND state='reserved'",
                       (str(chain), str(address).lower(), int(nonce)))

    # -- approvals ---------------------------------------------------------

    def find_approval(self, digest: str) -> Optional[Dict[str, Any]]:
        """The newest live row for *digest* (pending or granted, not expired)."""
        now = self._now()
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM approvals WHERE digest=? AND state IN ('pending','granted') "
                "AND expires > ? ORDER BY created DESC LIMIT 1", (digest, now)).fetchone()
        return dict(row) if row else None

    def open_approval(self, *, digest: str, op: str, chain: Optional[str],
                      amount_usd: float, summary: str, ttl_sec: int) -> Dict[str, Any]:
        existing = self.find_approval(digest)
        if existing is not None:
            return existing
        now = self._now()
        row = {"id": "sig-" + uuid.uuid4().hex[:10], "digest": digest, "op": op,
               "chain": chain, "amount_usd": float(amount_usd), "summary": summary[:400],
               "state": "pending", "created": now, "expires": now + int(ttl_sec),
               "decided_at": None, "decided_uid": None}
        with self._db() as db:
            db.execute("INSERT INTO approvals VALUES (:id,:digest,:op,:chain,:amount_usd,:summary,"
                       ":state,:created,:expires,:decided_at,:decided_uid)", row)
        return row

    def decide(self, approval_id: str, *, grant: bool, uid: int, ttl_sec: int) -> Optional[Dict[str, Any]]:
        now = self._now()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM approvals WHERE id=? AND state='pending' AND expires > ?",
                             (str(approval_id), now)).fetchone()
            if row is None:
                db.execute("ROLLBACK")
                return None
            # A grant is valid for ttl from the DECISION, so the agent has time
            # to retry after an owner who read the queue late.
            db.execute("UPDATE approvals SET state=?, decided_at=?, decided_uid=?, expires=? WHERE id=?",
                       ("granted" if grant else "denied", now, int(uid),
                        now + int(ttl_sec) if grant else row["expires"], row["id"]))
            db.execute("COMMIT")
        out = dict(row)
        out["state"] = "granted" if grant else "denied"
        return out

    def consume_grant(self, approval_id: str) -> bool:
        with self._db() as db:
            changed = db.execute(
                "UPDATE approvals SET state='consumed' WHERE id=? AND state='granted' AND expires > ?",
                (str(approval_id), self._now())).rowcount
        return changed == 1

    def pending(self) -> List[Dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM approvals WHERE state='pending' AND expires > ? "
                              "ORDER BY created", (self._now(),)).fetchall()
        return [dict(r) for r in rows]

    # -- pause -------------------------------------------------------------

    def paused(self) -> bool:
        with self._db() as db:
            row = db.execute("SELECT v FROM flags WHERE k='paused'").fetchone()
        return bool(row) and row["v"] == "1"

    def set_paused(self, paused: bool) -> None:
        with self._db() as db:
            db.execute("INSERT INTO flags VALUES ('paused', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                       ("1" if paused else "0",))

    # -- decisions ---------------------------------------------------------

    def log_decision(self, *, op: str, digest: Optional[str], allowed: bool,
                     code: Optional[str] = None, reason: Optional[str] = None,
                     peer_uid: Optional[int] = None, amount_usd: Optional[float] = None,
                     ref: Optional[str] = None) -> None:
        with self._db() as db:
            db.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?)",
                       (self._now(), op, digest, 1 if allowed else 0, code,
                        (reason or "")[:500], peer_uid, amount_usd, ref))
            db.execute("DELETE FROM decisions WHERE rowid IN (SELECT rowid FROM decisions "
                       "ORDER BY ts DESC LIMIT -1 OFFSET ?)", (_DECISIONS_KEPT,))

    def recent_decisions(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM decisions ORDER BY ts DESC LIMIT ?",
                              (int(limit),)).fetchall()
        return [dict(r) for r in rows]
