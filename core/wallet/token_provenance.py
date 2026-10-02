"""Tokens THIS instance launched or deployed — the ``own_launch`` trust source.

On 2026-09-25 the 6-hourly PNL buyback bought an airdropped look-alike, and from
then on the identity gate refused the REAL PNL every cycle: the wallet held a
tracked "PNL" at another address, and the only way to say which one was real was
an owner CLI pin on the server. But the real PNL is the instance's OWN Pons
launch — the chain already proves it (the factory records our wallet as its
deployer), and nothing fed that proof into ``tokens.get_token_identity``.

This module is that proof, as a small store beside ``token_pins.db``:

    <data_home>/wallet/token_provenance.db   (chain, address) -> kind, evidence

Writers — none of them is an agent verb, so the agent cannot vouch for a token
by itself:

* the launch / deploy verbs, on a CONFIRMED launch or deployment
  (``tools/launchpad/execute.py``, ``tools/defi/deploy_verb.py``);
* a one-time backfill from the append-only spend audit (``audit.jsonl``): a
  ``deploy_token`` row names the LANDED address (the verb records ``None`` when
  nothing landed); a ``launchpad_launch`` row counts only when a registered
  on-chain probe confirms it (a reverted launch records the factory);
* a registered on-chain probe (``register_launch_probe``) — the tools tier
  registers the Pons one: ``getLaunchedToken(token).deployer`` is one of our
  own addresses. Only the DEPLOYER counts: ``creatorFeeRecipient`` is chosen by
  whoever launches, so anyone could name us.

A read never creates the store. An unreadable store reads as "no own launches"
— the fail-closed direction for trust (it grants less, never more).
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from core import sqlite_util

logger = logging.getLogger(__name__)

SOURCE = "own_launch"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS own_tokens (
    chain       TEXT NOT NULL,
    address     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    evidence    TEXT NOT NULL DEFAULT '',
    recorded_ts REAL NOT NULL,
    PRIMARY KEY (chain, address)
)
"""

#: Audit actions whose ``counterparty`` is the landed token address.
_DEPLOY_ACTIONS = ("deploy_token",)
#: Audit actions whose ``counterparty`` is the token ONLY when the receipt named
#: one (else the factory) — a probe must confirm them.
_LAUNCH_ACTIONS = ("launchpad_launch",)

#: name -> fn(chain, address) -> evidence text, or None when not ours.
_PROBES: Dict[str, Callable[[str, str], Optional[str]]] = {}
#: (store path, chain, address) -> monotonic time of a NEGATIVE probe answer.
_NEGATIVE: Dict[Tuple[str, str, str], float] = {}
_NEGATIVE_TTL_S = 600.0
_BACKFILLED: set = set()


def provenance_db_path(data_home: Optional[str] = None) -> str:
    """``<data_home>/wallet/token_provenance.db`` — beside the pins and the audit."""
    if data_home:
        return os.path.join(str(data_home), "wallet", "token_provenance.db")
    from core.wallet.audit_sink import _wallet_data_dir
    return os.path.join(_wallet_data_dir(), "token_provenance.db")


def register_launch_probe(name: str, fn: Callable[[str, str], Optional[str]]) -> None:
    """Register an on-chain "did we launch this?" probe (last writer wins)."""
    if not callable(fn):
        raise TypeError(f"launch probe {name}: {fn!r} is not callable")
    _PROBES[str(name)] = fn


def _norm(chain: str, address: str) -> Optional[Tuple[str, str]]:
    chain = str(chain or "").strip().lower()
    try:
        from core.wallet.addresses import normalize_for_chain
        return chain, normalize_for_chain(chain, str(address or ""))
    except Exception:
        return None


def own_evm_addresses(data_home: Optional[str] = None) -> List[str]:
    """Every EVM address the wallet's public identity records (may be empty)."""
    try:
        from core.wallet.public_identity import read_public_identity
        record = read_public_identity(data_home)
    except Exception:
        return []
    evm = (record or {}).get("evm") or {}
    return [str(a) for a in evm.values() if isinstance(a, str) and a.startswith("0x")]


def record_own_token(chain: str, address: str, *, kind: str, evidence: str = "",
                     db_path: Optional[str] = None) -> bool:
    """Record *address* on *chain* as one this instance launched/deployed.

    Fail-open: a confirmed launch must never be reported as failed because this
    bookkeeping write did not land. Returns True when the row is stored.
    """
    key = _norm(chain, address)
    if key is None:
        return False
    path = db_path or provenance_db_path()
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite_util.wal_connect(path)
        try:
            conn.execute(_SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO own_tokens (chain, address, kind, evidence, "
                "recorded_ts) VALUES (?, ?, ?, ?, ?)",
                (key[0], key[1], str(kind), str(evidence or "")[:200], time.time()))
            conn.commit()
        finally:
            conn.close()
        _NEGATIVE.pop((path,) + key, None)
        return True
    except Exception:
        logger.warning("token provenance: could not record %s/%s", key[0], key[1],
                       exc_info=True)
        return False


def _read(key: Tuple[str, str], path: str) -> Optional[Dict[str, str]]:
    try:
        os.stat(path)
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning("token provenance store unreachable: %s", path)
        return None
    try:
        conn = sqlite_util.wal_connect(path)
        try:
            conn.execute(_SCHEMA)
            rows = conn.execute(
                "SELECT address, kind, evidence FROM own_tokens WHERE chain=?",
                (key[0],)).fetchall()
        finally:
            conn.close()
    except Exception:
        logger.warning("token provenance store unreadable: %s", path, exc_info=True)
        return None
    from core.wallet.addresses import same_address
    for r in rows:
        if same_address(r[0], key[1]):
            return {"chain": key[0], "address": r[0], "kind": r[1], "evidence": r[2]}
    return None


def _probe(key: Tuple[str, str]) -> Optional[str]:
    for name, fn in list(_PROBES.items()):
        try:
            evidence = fn(key[0], key[1])
        except Exception:
            logger.debug("launch probe %s failed for %s/%s", name, *key, exc_info=True)
            continue
        if evidence:
            return f"{name}: {evidence}"
    return None


def backfill_from_audit(*, audit_path: Optional[str] = None,
                        db_path: Optional[str] = None) -> int:
    """Record every own launch/deploy the spend audit proves. Returns the count.

    Read-only on the audit. A ``deploy_token`` row is recorded as written (its
    counterparty is the LANDED address, or null); a ``launchpad_launch`` row
    only when a registered probe confirms it on-chain.
    """
    if audit_path is None:
        try:
            from core.wallet.trade_index import audit_path as _ap
            audit_path = _ap()
        except Exception:
            return 0
    if not os.path.isfile(audit_path):
        return 0
    found = 0
    try:
        with open(audit_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return 0
    for line in lines:
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        action = str(entry.get("action") or "")
        token = entry.get("counterparty")
        chain = entry.get("chain")
        if not token or not chain or action not in _DEPLOY_ACTIONS + _LAUNCH_ACTIONS:
            continue
        key = _norm(chain, token)
        if key is None:
            continue
        ref = str(entry.get("result_ref") or "")
        if action in _DEPLOY_ACTIONS:
            if record_own_token(*key, kind=action, evidence=f"audit tx {ref}",
                                db_path=db_path):
                found += 1
            continue
        evidence = _probe(key)
        if evidence and record_own_token(*key, kind=action,
                                         evidence=f"audit tx {ref}; {evidence}",
                                         db_path=db_path):
            found += 1
    return found


def own_token(chain: str, address: str, *, probe: bool = False,
              db_path: Optional[str] = None,
              audit_path: Optional[str] = None) -> Optional[Dict[str, str]]:
    """The provenance row for ``(chain, address)``, or None when not ours.

    ``probe=True`` (the spend path) also asks the registered on-chain probes on a
    miss and records a positive answer, so every later read is a plain lookup.
    """
    key = _norm(chain, address)
    if key is None:
        return None
    path = db_path or _safe_default_path()
    if path is None:
        return None
    row = _read(key, path)
    if row is not None:
        return row
    if path not in _BACKFILLED:
        _BACKFILLED.add(path)
        try:
            if backfill_from_audit(audit_path=audit_path, db_path=path):
                row = _read(key, path)
                if row is not None:
                    return row
        except Exception:
            logger.debug("token provenance backfill failed", exc_info=True)
    if not probe or not _PROBES:
        return None
    seen = _NEGATIVE.get((path,) + key)
    if seen is not None and time.monotonic() - seen < _NEGATIVE_TTL_S:
        return None
    evidence = _probe(key)
    if not evidence:
        _NEGATIVE[(path,) + key] = time.monotonic()
        return None
    record_own_token(*key, kind="onchain_probe", evidence=evidence, db_path=path)
    return {"chain": key[0], "address": key[1], "kind": "onchain_probe",
            "evidence": evidence}


def all_own_tokens(*, db_path: Optional[str] = None) -> List[Dict[str, str]]:
    """Every recorded own launch/deploy (W1: the owner's trusted-token list).
    Read-only; an absent or unreadable store reads as none."""
    path = db_path or _safe_default_path()
    if not path:
        return []
    try:
        os.stat(path)
    except OSError:
        return []
    try:
        conn = sqlite_util.wal_connect(path)
        try:
            conn.execute(_SCHEMA)
            rows = conn.execute(
                "SELECT chain, address, kind, evidence FROM own_tokens "
                "ORDER BY chain, recorded_ts").fetchall()
        finally:
            conn.close()
    except Exception:
        logger.warning("token provenance store unreadable: %s", path, exc_info=True)
        return []
    return [{"chain": r[0], "address": r[1], "kind": r[2], "evidence": r[3]}
            for r in rows]


def _safe_default_path() -> Optional[str]:
    try:
        return provenance_db_path()
    except Exception:
        return None


def _reset_for_tests() -> None:
    _NEGATIVE.clear()
    _BACKFILLED.clear()
