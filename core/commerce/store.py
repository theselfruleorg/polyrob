"""``commerce.db`` — offers, their pinned versions and their resources (099 W1, §3.3).

One row per offer (``offers``): its working manifest (the draft or a pending
edit), its state, and — once live — the version the owner confirmed. Every
confirmed version is kept in ``offer_versions``; an order pins one and reads
that snapshot forever, whatever the offer becomes.

Who may do what:

* the agent drafts and edits (:func:`save_draft`), records resources
  (:func:`add_resource`) and test evidence (:func:`record_test`), and may PAUSE;
* only the owner seat (``actor == "owner_seat"``, stamped by a card Confirm)
  starts setup, takes an offer live, resumes or retires it.

The live offer IS the commerce mandate: its confirmed version pins the tools a
fulfillment run loads, the per-order spend budget and the resource contracts
the money kernel admits (``core.money.offer_gate``). There is no second
mandate row to drift from it.

⚠️ Resources live HERE only. A contract resource is never mirrored into
``own_tokens``: that table is the ``own_launch`` trust source, and an offer
contract is not a token the agent launched.

⚠️ An unreadable store is not an empty one: a read of an existing file that
fails raises :class:`CommerceUnreadable`; a READ never creates the file.

Tier-0: imports nothing above ``core``.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from core.commerce.offer import OfferError, OfferManifest, validate
from core.state.machine import StateMachine

DB_NAME = "commerce.db"
OWNER_SEAT = "owner_seat"

DRAFT, SETUP, LIVE, PAUSED, RETIRED = "draft", "setup", "live", "paused", "retired"

OFFER_MACHINE = StateMachine(
    "commerce_offer", states=frozenset({DRAFT, SETUP, LIVE, PAUSED, RETIRED}),
    initial=frozenset({DRAFT}), terminal=frozenset({RETIRED}),
    transitions={DRAFT: {SETUP, LIVE, RETIRED},
                 SETUP: {DRAFT, LIVE, RETIRED},
                 LIVE: {PAUSED, RETIRED},
                 PAUSED: {LIVE, RETIRED}})

RESOURCE_KINDS = ("contract", "file_set", "account", "endpoint", "storage")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS offers (
    id               TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL,
    state            TEXT NOT NULL,
    manifest         TEXT NOT NULL,
    version          INTEGER NOT NULL DEFAULT 0,
    setup_budget_usd REAL,
    test_evidence    TEXT,
    actor            TEXT NOT NULL,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS offer_versions (
    offer_id      TEXT NOT NULL,
    version       INTEGER NOT NULL,
    manifest      TEXT NOT NULL,
    playbook_sha  TEXT,
    contracts     TEXT NOT NULL DEFAULT '[]',
    confirmed_at  REAL NOT NULL,
    PRIMARY KEY (offer_id, version)
);
CREATE TABLE IF NOT EXISTS offer_resources (
    id          TEXT PRIMARY KEY,
    offer_id    TEXT NOT NULL,
    kind        TEXT NOT NULL,
    chain       TEXT,
    address     TEXT,
    data        TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_offer_resources ON offer_resources(offer_id);
"""


class CommerceUnreadable(RuntimeError):
    """The store exists and could not be read. Never read this as "no offer"."""


class CommerceError(ValueError):
    """A write the rules refuse (wrong actor, wrong state, bad field)."""


@dataclass(frozen=True)
class Offer:
    id: str
    user_id: str
    state: str
    manifest: Dict[str, Any]          # the working copy (draft / pending edit)
    version: int                      # the live version; 0 = never live
    actor: str
    created_at: float
    updated_at: float
    setup_budget_usd: Optional[float] = None
    test_evidence: Optional[Dict[str, Any]] = None

    @property
    def is_live(self) -> bool:
        return self.state == LIVE

    def working(self) -> OfferManifest:
        return validate(self.manifest)


@dataclass(frozen=True)
class Resource:
    id: str
    offer_id: str
    kind: str
    chain: Optional[str]
    address: Optional[str]
    data: Mapping[str, Any] = field(default_factory=dict)
    created_at: float = 0.0


# -- paths + reads -------------------------------------------------------------

def db_path(data_dir: Optional[str] = None) -> str:
    """``<data_home>/commerce.db``, resolved at CALL time."""
    from core.runtime_paths import data_home_db_path
    return data_home_db_path(DB_NAME, data_dir=data_dir)


def init_schema(path: Optional[str] = None) -> str:
    """Create the store (the WRITE path only). Returns the path."""
    from core.sqlite_util import init_schema as _init
    from core.commerce.orders import ORDER_SCHEMA
    p = path or db_path()
    _init(p, _SCHEMA + ORDER_SCHEMA, mkdir=True,
          columns={"offer_orders": (("payment_tx", "TEXT"),),
                   "offer_versions": (("contracts", "TEXT NOT NULL DEFAULT '[]'"),)})
    return p


def read(path: Optional[str], sql: str, args: tuple, fetch: str):
    """A read that never creates the file: absent = ``None``/``[]``."""
    p = path or db_path()
    try:
        os.stat(p)
    except FileNotFoundError:
        return [] if fetch == "all" else None
    except OSError as exc:
        raise CommerceUnreadable(f"commerce store unreachable: {exc}") from exc
    try:
        from core.sqlite_util import execute_retry
        return execute_retry(p, sql, args, fetch=fetch)
    except Exception as exc:
        if "no such table" in str(exc):
            return [] if fetch == "all" else None
        raise CommerceUnreadable(f"commerce store unreadable: {exc}") from exc


def _row(r) -> Offer:
    return Offer(id=r["id"], user_id=r["user_id"], state=r["state"],
                 manifest=json.loads(r["manifest"] or "{}"), version=int(r["version"] or 0),
                 actor=r["actor"], created_at=float(r["created_at"]),
                 updated_at=float(r["updated_at"]),
                 setup_budget_usd=r["setup_budget_usd"],
                 test_evidence=json.loads(r["test_evidence"]) if r["test_evidence"] else None)


def get(offer_id: str, *, path: Optional[str] = None) -> Optional[Offer]:
    r = read(path, "SELECT * FROM offers WHERE id=?", (str(offer_id).lower(),), "one")
    return _row(r) if r else None


def list_offers(user_id: Optional[str] = None, *, states=None,
                path: Optional[str] = None) -> List[Offer]:
    sql, args = "SELECT * FROM offers WHERE 1=1", []
    if user_id:
        sql += " AND user_id=?"
        args.append(str(user_id))
    if states:
        states = list(states)
        sql += f" AND state IN ({','.join('?' * len(states))})"
        args += states
    rows = read(path, sql + " ORDER BY created_at", tuple(args), "all") or []
    return [_row(r) for r in rows]


def live_version(offer_id: str, version: Optional[int] = None, *,
                 path: Optional[str] = None) -> Optional[OfferManifest]:
    """The confirmed manifest of *version* (default: the live one), or ``None``."""
    if version is None:
        o = get(offer_id, path=path)
        if o is None or not o.version:
            return None
        version = o.version
    r = read(path, "SELECT manifest FROM offer_versions WHERE offer_id=? AND version=?",
             (str(offer_id).lower(), int(version)), "one")
    return validate(r["manifest"]) if r else None


def playbook_sha(offer_id: str, version: int, *, path: Optional[str] = None) -> Optional[str]:
    r = read(path, "SELECT playbook_sha FROM offer_versions WHERE offer_id=? AND version=?",
             (str(offer_id).lower(), int(version)), "one")
    return r["playbook_sha"] if r else None


def live_offers(*, listing: Optional[str] = None,
                path: Optional[str] = None) -> List[OfferManifest]:
    """The confirmed manifest of every LIVE offer (optionally: that lists *listing*)."""
    out = []
    for o in list_offers(states=[LIVE], path=path):
        m = live_version(o.id, o.version, path=path)
        if m is not None and (listing is None or listing in m.listings):
            out.append(m)
    return out


def owner_of(offer_id: str, *, path: Optional[str] = None) -> Optional[str]:
    o = get(offer_id, path=path)
    return o.user_id if o else None


# -- writes --------------------------------------------------------------------

def _now(now: Optional[float]) -> float:
    return time.time() if now is None else float(now)


def save_draft(user_id: str, manifest: Mapping[str, Any], *, actor: str = "agent",
               path: Optional[str] = None, now: Optional[float] = None) -> Offer:
    """Create the draft, or replace the working manifest of a draft/setup offer or
    stage an edit of a live/paused one (the live version is untouched until the
    owner confirms it). Raises :class:`OfferError` / :class:`CommerceError`."""
    m = validate(manifest)
    uid = str(user_id or "").strip()
    if not uid:
        raise CommerceError("an offer needs an owner (user_id)")
    p = init_schema(path)
    from core.sqlite_util import execute_retry
    ts = _now(now)
    cur = get(m.id, path=p)
    if cur is None:
        execute_retry(p, "INSERT INTO offers (id, user_id, state, manifest, version, actor, "
                         "created_at, updated_at) VALUES (?,?,?,?,0,?,?,?)",
                      (m.id, uid, DRAFT, m.to_json(), str(actor), ts, ts))
    else:
        if cur.user_id != uid:
            raise CommerceError(f"offer {m.id} belongs to another tenant")
        if cur.state == RETIRED:
            raise CommerceError(f"offer {m.id} is retired — draft it under a new id")
        n = execute_retry(p, "UPDATE offers SET manifest=?, updated_at=? WHERE id=? AND "
                             "state=?", (m.to_json(), ts, m.id, cur.state))
        if n != 1:
            raise CommerceError(f"offer {m.id} changed while it was being saved; try again")
    return get(m.id, path=p)


def _owner(actor: str, what: str) -> None:
    if actor != OWNER_SEAT:
        raise CommerceError(f"only the owner {what} an offer (the card Confirm)")


def _move(offer: Offer, to: str, *, path: str, now: float, set_cols=None) -> Offer:
    from core.state.sql import cas_transition
    if not cas_transition(path, "offers", key="id=?", key_params=(offer.id,),
                          machine=OFFER_MACHINE, to=to, from_states=[offer.state],
                          set_cols={"updated_at": now, **(set_cols or {})},
                          status_col="state"):
        raise CommerceError(f"offer {offer.id} is {offer.state}; it cannot become {to}")
    return get(offer.id, path=path)


def _require(offer_id: str, user_id: str, path: str) -> Offer:
    o = get(offer_id, path=path)
    if o is None or o.user_id != str(user_id):
        raise CommerceError(f"no offer {offer_id!r}")
    return o


def start_setup(offer_id: str, user_id: str, *, budget_usd: float, actor: str,
                path: Optional[str] = None, now: Optional[float] = None) -> Offer:
    """draft -> setup with the owner's ONE setup budget (U39)."""
    _owner(actor, "starts the setup of")
    p = init_schema(path)
    o = _require(offer_id, user_id, p)
    try:
        b = float(budget_usd)
    except (TypeError, ValueError):
        raise CommerceError("the setup budget must be a number of dollars") from None
    if not 0 <= b <= 500:
        raise CommerceError("the setup budget must be between $0 and $500")
    return _move(o, SETUP, path=p, now=_now(now), set_cols={"setup_budget_usd": b})


def record_test(offer_id: str, user_id: str, evidence: Mapping[str, Any], *,
                path: Optional[str] = None, now: Optional[float] = None) -> Offer:
    p = init_schema(path)
    o = _require(offer_id, user_id, p)
    if o.state == RETIRED:
        raise CommerceError(f"offer {offer_id} is retired")
    from core.sqlite_util import execute_retry
    body = json.dumps(dict(evidence or {}), default=str)[:8000]
    execute_retry(p, "UPDATE offers SET test_evidence=?, updated_at=? WHERE id=?",
                  (body, _now(now), o.id))
    return get(o.id, path=p)


def manifest_hash(manifest: Mapping[str, Any],
                  contracts: Optional[Mapping[str, Any]] = None) -> str:
    """The short hash a live card pins: Confirm goes live only on THIS manifest and
    (``contracts``) THESE contract resources and functions."""
    body = validate(manifest).to_json()
    if contracts:
        body += json.dumps(dict(contracts), sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()[:12]


def _contracts(offer_id: str, path: Optional[str]) -> Dict[str, List[str]]:
    """``{"chain:address": [selector…]}`` of the offer's contract resources — what a
    go-live pins to the version."""
    return {f"{r.chain}:{r.address}": sorted(selector(f) for f in r.data.get("functions") or ())
            for r in resources(offer_id, kind="contract", path=path)}


def card_hash(offer: "Offer", *, path: Optional[str] = None) -> str:
    """The hash the go-live card shows: the manifest AND the contract resources the
    card listed, so a resource added after the card was sent refuses the Confirm."""
    return manifest_hash(offer.manifest, _contracts(offer.id, path))


def go_live(offer_id: str, user_id: str, *, actor: str, expect_hash: str,
            playbook_sha: Optional[str] = None, path: Optional[str] = None,
            now: Optional[float] = None) -> Offer:
    """Confirm the working manifest as the next live version (owner only).

    ``expect_hash`` is the :func:`manifest_hash` the card showed: an edit made
    after the card was sent refuses, so the owner never confirms words they did
    not read."""
    _owner(actor, "takes live")
    p = init_schema(path)
    o = _require(offer_id, user_id, p)
    # The contract resources the owner saw are PINNED to this version: one added later
    # gains no authority, and one added after the card was sent changes the hash.
    pinned = _contracts(o.id, p)
    if manifest_hash(o.manifest, pinned) != str(expect_hash or ""):
        raise CommerceError(f"offer {o.id} changed after this card was made — the agent "
                            f"must propose it again")
    m = o.working()
    ts = _now(now)
    version = o.version + 1
    from core.sqlite_util import execute_retry
    execute_retry(p, "INSERT INTO offer_versions (offer_id, version, manifest, playbook_sha, "
                     "contracts, confirmed_at) VALUES (?,?,?,?,?,?)",
                  (o.id, version, m.to_json(), playbook_sha,
                   json.dumps(pinned, sort_keys=True), ts))
    if o.state == LIVE:
        n = execute_retry(p, "UPDATE offers SET version=?, actor=?, updated_at=? WHERE id=? "
                             "AND version=? AND state=?",
                          (version, actor, ts, o.id, o.version, LIVE))
        if n != 1:
            raise CommerceError(f"offer {o.id} changed while going live; try again")
        return get(o.id, path=p)
    return _move(o, LIVE, path=p, now=ts, set_cols={"version": version, "actor": actor})


def set_state(offer_id: str, user_id: str, to: str, *, actor: str,
              path: Optional[str] = None, now: Optional[float] = None) -> Offer:
    """pause (anyone: it only stops sales) / resume / retire (owner)."""
    to = str(to).lower()
    if to not in (PAUSED, LIVE, RETIRED):
        raise CommerceError("an offer can be paused, resumed or retired")
    if to != PAUSED:
        _owner(actor, "resumes or retires")
    p = init_schema(path)
    o = _require(offer_id, user_id, p)
    if to == LIVE and (o.state != PAUSED or not o.version):
        raise CommerceError(f"offer {o.id} is {o.state}; only a paused live offer resumes")
    return _move(o, to, path=p, now=_now(now))


def add_resource(offer_id: str, user_id: str, kind: str, data: Mapping[str, Any], *,
                 chain: Optional[str] = None, address: Optional[str] = None,
                 path: Optional[str] = None, now: Optional[float] = None) -> Resource:
    """Record a provisioned thing. A ``contract`` needs a chain, an address and the
    runtime code hash the deploy recorded (the money kernel pins the code too)."""
    kind = str(kind or "").lower()
    if kind not in RESOURCE_KINDS:
        raise CommerceError(f"resource kind must be one of {', '.join(RESOURCE_KINDS)}")
    data = dict(data or {})
    if any(k in data for k in ("secret", "private_key", "password", "seed", "api_key")):
        raise CommerceError("a resource records a secret REF, never the secret")
    if kind == "contract":
        import re
        if not chain or not address or not re.fullmatch(r"0x[0-9a-fA-F]{40}", str(address)):
            raise CommerceError("a contract resource needs a chain and a 0x address")
        if not re.fullmatch(r"0x[0-9a-f]{64}", str(data.get("runtime_hash") or "").lower()):
            raise CommerceError("a contract resource needs runtime_hash (keccak of the "
                                "deployed code, 0x + 64 hex)")
        data["functions"] = _functions(data.get("functions"))
        address = str(address).lower()
    p = init_schema(path)
    o = _require(offer_id, user_id, p)
    if o.state == RETIRED:
        raise CommerceError(f"offer {offer_id} is retired")
    rid = f"res_{uuid.uuid4().hex[:10]}"
    from core.sqlite_util import execute_retry
    execute_retry(p, "INSERT INTO offer_resources (id, offer_id, kind, chain, address, data, "
                     "created_at) VALUES (?,?,?,?,?,?,?)",
                  (rid, o.id, kind, (chain or None) and str(chain).lower(), address,
                   json.dumps(data, default=str)[:4000], _now(now)))
    return Resource(id=rid, offer_id=o.id, kind=kind, chain=chain, address=address, data=data)


def _functions(raw: Any) -> List[str]:
    """The function signatures a fulfillment may call on a contract resource, checked.
    ``["mint(address,string)"]`` — each becomes a selector pinned at go-live; a call
    to any other function (``transferOwnership``, ``withdraw``, ``upgradeTo``…) refuses."""
    import re
    sigs = [str(x).replace(" ", "") for x in (raw or ()) if str(x).strip()]
    if not sigs or len(sigs) > 8:
        raise CommerceError("a contract resource names 1-8 callable functions, e.g. "
                            "functions=[\"mint(address,string)\"] — nothing else is callable")
    for sig in sigs:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\([A-Za-z0-9_,\[\]()]*\)", sig):
            raise CommerceError(f"{sig!r} is not a function signature like mint(address,string)")
    return sigs


def selector(signature: str) -> str:
    from eth_utils import keccak
    return "0x" + keccak(text=str(signature).replace(" ", "")).hex()[:8]


def resources(offer_id: str, *, kind: Optional[str] = None,
              path: Optional[str] = None) -> List[Resource]:
    sql, args = "SELECT * FROM offer_resources WHERE offer_id=?", [str(offer_id).lower()]
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    rows = read(path, sql + " ORDER BY created_at", tuple(args), "all") or []
    return [Resource(id=r["id"], offer_id=r["offer_id"], kind=r["kind"], chain=r["chain"],
                     address=r["address"], data=json.loads(r["data"] or "{}"),
                     created_at=float(r["created_at"])) for r in rows]


def pinned_contract(offer_id: str, version: int, chain: str, address: str, *,
                    path: Optional[str] = None) -> Optional[List[str]]:
    """The callable selectors of (*chain*, *address*) when it is a contract resource the
    owner CONFIRMED with *version* (the set :func:`go_live` pinned), else ``None``."""
    r = read(path, "SELECT contracts FROM offer_versions WHERE offer_id=? AND version=?",
             (str(offer_id).lower(), int(version)), "one")
    if not r:
        return None
    pinned = json.loads(r["contracts"] or "{}")
    if not isinstance(pinned, dict):
        return None
    sels = pinned.get(f"{str(chain).lower()}:{str(address).lower()}")
    return list(sels) if sels is not None else None


def contract_resource(offer_id: str, chain: str, address: str, *,
                      path: Optional[str] = None) -> Optional[Resource]:
    """The offer's contract resource at (*chain*, *address*), or ``None``."""
    for r in resources(offer_id, kind="contract", path=path):
        if (r.chain or "") == str(chain).lower() and (r.address or "") == str(address).lower():
            return r
    return None


__all__ = ["CommerceError", "CommerceUnreadable", "DRAFT", "LIVE", "OFFER_MACHINE",
           "OWNER_SEAT", "Offer", "PAUSED", "RETIRED", "Resource", "SETUP", "add_resource",
           "contract_resource", "db_path", "pinned_contract", "selector", "get", "go_live", "init_schema", "list_offers",
           "live_offers", "live_version", "manifest_hash", "card_hash", "record_test", "resources",
           "save_draft", "set_state", "start_setup"]
