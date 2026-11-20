"""The ONE standing owner grant — the mandate (proposal 122 section 3, 124 section 5.2, P1).

A mandate is one owner approval with a scope, confirmed from the owner seat only. Inside the
scope, an act that would wait for the owner's card (an ``owner_always`` verb, a payout, an act
through an unpinned NFT account) runs with no card; outside it, the card returns. It never
widens a cap: the pause, the per-transaction and daily caps and the guard's simulation bind
every mandate act. ``/nft trust <collection> on <chain> autonomous <acts> band <min>-<max>``
writes a mandate scoped to that collection — there is no second store.

Scope (every field required when the mandate is made):

* ``verbs``     — the action names it covers;
* ``chains``    — where;
* ``assets``    — collection / token addresses the act must name (empty = none named);
* ``band_usd``  — ``(min, max)`` of the act's declared ``max_spend_usd``;
* ``count``     — how many acts, ``total_usd`` — their declared sum;
* ``ends_at``   — at most :data:`MAX_DAYS` days;
* ``recipients``— the ``to`` a send may name (empty = a send names none);
* ``facts``     — per collection, the chain facts at the time of trust (code hash, ``SALE()``,
  ``MARKET()``): a change drops the act back to the card (124 section 5.2, O3).

Use: :func:`exemption` matches an act and RESERVES one use (count and declared USD) under a
file lock before the act runs, so a used count is never replayed and a crash after the
reservation spends a use, never a second act. When the guard refuses the act's first live
transaction (nothing signed), :func:`settle_guard` gives the use back once. A mandate ends at ``ends_at``, at ``/nft mandate
end <id>``, or when its count or total is used. ``/pause`` stops every mandate act (the kernel's
pause step runs first).

Who may use one: only an autonomous MAIN-agent run (a goal or a cron job) that
the caller proves (``autonomous=True``). A correspondent, room, leaf or sub-agent turn never uses
a mandate — the callers check the turn; this module refuses when the caller does not vouch.

The store: ``<wallet data dir>/mandates.json``. An unreadable store is NO mandate (every act
falls back to the card) and is logged; it never opens a money path. A store that holds one id
twice is unreadable. A rollback (``polyrob update rollback``) keeps the live file
(``core.update_snapshot.context.ROLLBACK_KEPT_FILES``): a restore never un-ends a mandate or
resets its use count.

It sits beside the wallet (its facts check reads the chain). ONE caller asks it: the
controller's owner-queue hook (``tools/controller/self_transfer.py::mandate_exemption``),
which also stamps the owner's grant for that call so the guard's card rules read it.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.state.files import atomic_write_json, file_lock
from core.wallet import owner_signed

logger = logging.getLogger(__name__)

STORE_NAME = "mandates.json"
#: The longest a mandate may live (122 section 3).
MAX_DAYS = 30
#: The NFT acts ``/nft trust … autonomous`` can name, and the action names each covers. The
#: sale acts (ready, list, accept) stay card-only: the account's whole holdings leave with the
#: NFT and a declared USD bound does not measure them (the 2026-11-20 review).
NFT_ACTS: Dict[str, Tuple[str, ...]] = {
    "mint": ("agent_nft_collection_mint",),
    "reveal": ("agent_nft_collection_reveal",),
    "buy": ("agent_nft_buy",),
    "account": ("defi_trade_transfer", "defi_trade_swap", "defi_trade_call",
                "launchpad_claim"),
}


@dataclass
class Mandate:
    id: str
    created_at: float
    ends_at: float
    verbs: List[str]
    chains: List[str]
    assets: List[str]
    band_usd: Tuple[float, float]
    count: int
    total_usd: float
    recipients: List[str] = field(default_factory=list)
    facts: Dict[str, Dict[str, str]] = field(default_factory=dict)
    note: str = ""
    used_count: int = 0
    used_usd: float = 0.0
    ended_at: Optional[float] = None
    #: The wallet's signature over every other field (re-signed on each write): a mandate a
    #: shell wrote, or one whose count was reset, does not verify and grants nothing.
    sig: str = ""

    def live(self, now: float) -> bool:
        return (self.ended_at is None and now < self.ends_at and self.used_count < self.count
                and self.used_usd < self.total_usd)

    def line(self, now: Optional[float] = None) -> str:
        now = time.time() if now is None else now
        state = ("ended" if self.ended_at is not None else "expired" if now >= self.ends_at
                 else "used up" if not self.live(now) else "live")
        days = max(0.0, (self.ends_at - now) / 86_400)
        return (f"{self.id} [{state}] {', '.join(self.verbs)} on {', '.join(self.chains)}"
                + (f" for {', '.join(self.assets)}" if self.assets else "")
                + f" — ${self.band_usd[0]:g}-${self.band_usd[1]:g} an act, "
                f"{self.used_count}/{self.count} acts, ${self.used_usd:.2f}/${self.total_usd:g}, "
                f"{days:.1f} days left" + (f" ({self.note})" if self.note else ""))


class MandateError(ValueError):
    """A mandate cannot be made as asked; the text says why."""


# ---------------------------------------------------------------------------- the store

def store_path(data_home: Optional[str] = None) -> str:
    if data_home:
        return os.path.join(str(data_home), "wallet", STORE_NAME)
    from core.wallet.audit_sink import _wallet_data_dir
    return os.path.join(_wallet_data_dir(), STORE_NAME)


def _read(path: str) -> List[Mandate]:
    """The mandates; no file = none. An unreadable file RAISES (never "none")."""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict) or not isinstance(raw.get("mandates"), list):
        raise ValueError(f"{path} is not a mandate store")
    out, ids = [], set()
    for row in raw["mandates"]:
        row = dict(row)
        row["band_usd"] = tuple(float(x) for x in row.get("band_usd") or (0, 0))
        m = Mandate(**row)
        # A signed row copied N times verifies N times: the copies would multiply its count
        # and total. The store is refused as a whole (audit 2026-11-21 M-5).
        if m.id in ids:
            raise ValueError(f"{path} holds mandate {m.id} twice (duplicate id)")
        ids.add(m.id)
        out.append(m)
    return out


def _write(path: str, rows: List[Mandate]) -> None:
    atomic_write_json(path, {"version": 1, "mandates": [asdict(m) for m in rows]}, indent=1)


#: What the wallet signs: this prefix and the sha256 of the record (``wallet_sign`` signs only
#: classified login texts, so the agent cannot have it signed through that tool).
SIG_PREFIX = b"polyrob owner mandate v1:"


#: The wallet key that signs a mandate (``core.wallet.owner_signed``); tests replace it.
_signer = owner_signed.wallet_signer


def _sign(m: Mandate) -> None:
    owner_signed.sign(m, SIG_PREFIX, _signer(), MandateError, noun="the mandate",
                      remote="no mandate can be made or used yet; every act keeps its card")


def _verified(m: Mandate) -> bool:
    return owner_signed.verified(m, SIG_PREFIX, _signer())


def mandates(path: Optional[str] = None) -> List[Mandate]:
    """Every mandate (live or not). Raises when the store is unreadable."""
    return _read(path or store_path())


# ---------------------------------------------------------------------------- the owner's acts

def _owner_refusal(ctx: Any, verb: str) -> Optional[str]:
    return owner_signed.seat_refusal(
        ctx, verb, does="grants standing money authority",
        public="grant standing money authority",
        denied="only the owner seat makes or ends a mandate (the agent cannot)")


def create(ctx: Any, *, verbs, chains, band_usd, count: int, total_usd: float, days: float,
           assets=(), recipients=(), facts: Optional[Dict[str, Dict[str, str]]] = None,
           note: str = "", path: Optional[str] = None, now: Optional[float] = None) -> Mandate:
    """Make a mandate (the owner seat only). Raises :class:`MandateError` naming the field."""
    refusal = _owner_refusal(ctx, "make a mandate")
    if refusal:
        raise MandateError(refusal)
    now = time.time() if now is None else now
    verbs = sorted({str(v).strip() for v in verbs if str(v).strip()})
    chains = sorted({str(c).strip().lower() for c in chains if str(c).strip()})
    if not verbs:
        raise MandateError("a mandate names the verbs it covers")
    if not chains:
        raise MandateError("a mandate names its chains")
    lo, hi = (float(band_usd[0]), float(band_usd[1]))
    if not 0 <= lo <= hi or hi <= 0:
        raise MandateError(f"the band {lo:g}-{hi:g} is not a USD range (min ≤ max, max > 0)")
    if int(count) < 1:
        raise MandateError("a mandate allows at least one act")
    if float(total_usd) < hi:
        raise MandateError(f"the total ${float(total_usd):g} is below one act's max ${hi:g}")
    if not 0 < float(days) <= MAX_DAYS:
        raise MandateError(f"a mandate lasts more than 0 and at most {MAX_DAYS} days")
    m = Mandate(id="m_" + secrets.token_hex(4), created_at=now, ends_at=now + float(days) * 86_400,
                verbs=verbs, chains=chains,
                assets=sorted({str(a).lower() for a in assets if str(a).strip()}),
                band_usd=(lo, hi), count=int(count), total_usd=float(total_usd),
                recipients=sorted({str(r).lower() for r in recipients if str(r).strip()}),
                facts={str(k).lower(): dict(v) for k, v in (facts or {}).items()}, note=note[:200])
    _sign(m)
    p = path or store_path()
    with file_lock(p):
        rows = _read(p)
        rows.append(m)
        _write(p, rows)
    logger.info("mandate %s made: %s", m.id, m.line(now))
    return m


def end(ctx: Any, mandate_id: str, *, path: Optional[str] = None,
        now: Optional[float] = None) -> Mandate:
    refusal = _owner_refusal(ctx, "end a mandate")
    if refusal:
        raise MandateError(refusal)
    now = time.time() if now is None else now
    p = path or store_path()
    with file_lock(p):
        rows = _read(p)
        hit = next((m for m in rows if m.id == str(mandate_id).strip()), None)
        if hit is None:
            raise MandateError(f"no mandate {mandate_id!r}")
        if hit.ended_at is None:
            hit.ended_at = now
            try:
                _sign(hit)
            except MandateError:
                hit.sig = ""     # an ended mandate that does not verify grants nothing either
        _write(p, rows)
    return hit


# ---------------------------------------------------------------------------- the match

#: The params that name an ASSET the act touches (every one must be in the scope's assets).
_ASSET_KEYS = ("collection", "token", "contract", "token_in", "token_out", "spend_token",
               "receive_token", "quote_token")   # quote_token: a launch's pair (122 W3)


def _named(params: Dict[str, Any]) -> List[str]:
    """Every asset address the act's params name (``to`` and ``account`` are not assets)."""
    out = []
    for key in _ASSET_KEYS:
        v = params.get(key)
        if isinstance(v, str) and v.startswith("0x") and len(v) == 42:
            out.append(v.lower())
    nft = params.get("nft")
    if isinstance(nft, str) and "#" in nft and nft.split("#", 1)[0].startswith("0x"):
        out.append(nft.split("#", 1)[0].lower())
    return out


def covers(m: Mandate, action: str, params: Dict[str, Any], now: float) -> Optional[str]:
    """None when *m* covers this act exactly, else why not (for the log)."""
    if not m.live(now):
        return "not live"
    if action not in m.verbs:
        return f"{action} is not in its verbs"
    chain = str(params.get("chain") or "").lower()
    if chain not in m.chains:
        return f"chain {chain or '?'} is not in its chains"
    from core.wallet.collection_reveal import declared_usd
    declared = declared_usd(action, params)   # a reveal: the operator's fee cap
    if declared is None:
        return "the act declares no max_spend_usd"
    if not m.band_usd[0] <= float(declared) <= m.band_usd[1]:
        return f"${float(declared):g} is outside its band"
    if m.used_usd + float(declared) > m.total_usd + 1e-9:
        return "its total would be passed"
    # The review (2026-11-20): EVERY asset the act names is in scope, and at least one is;
    # a recipient (`to`) must be in the recipients — never "an asset".
    named = _named(params)
    if not named or any(a not in m.assets for a in named):
        return "the act names an asset outside its assets (or none)"
    to = params.get("to")
    if isinstance(to, str) and to.strip() and to.lower() not in m.recipients:
        return f"the recipient {to} is not in its recipients"
    for key in ("args", "allow_spender", "spender"):
        if params.get(key):
            return f"the act carries `{key}`, which a mandate never covers"
    return None


def _facts_changed(m: Mandate, rpc_fn: Optional[Callable[[str], Any]]) -> Optional[str]:
    """124 section 5.2 / O3: the chain facts recorded at trust time still hold (the code hash,
    and ``SALE()`` / ``MARKET()`` when recorded). A change — or a read that fails — drops the
    act back to the card."""
    if not m.facts:
        return None
    if rpc_fn is None:
        return "no RPC to re-read the collection's facts"
    from core.wallet import collection_registry, collection_sale
    for collection, facts in m.facts.items():
        chain = m.chains[0]
        try:
            rpc = rpc_fn(chain)
            if facts.get("runtime_sha256") and collection_registry.runtime_sha256_of(
                    rpc, collection) != facts["runtime_sha256"]:
                return f"the code of {collection} changed since the trust"
            if facts.get("sale"):
                now_sale = collection_sale._addr(rpc, collection, collection_sale.SALE_OF_SELECTOR)
                if now_sale != facts["sale"].lower():
                    return f"SALE() of {collection} changed since the trust"
            if facts.get("implementation_slot"):
                slot = rpc("eth_getStorageAt", [collection, _ERC1967_SLOT, "latest"])
                if str(slot).lower() != facts["implementation_slot"].lower():
                    return f"the proxy implementation of {collection} changed since the trust"
        except Exception as exc:  # noqa: BLE001
            return f"the facts of {collection} could not be re-read ({exc})"
    return None


#: The ERC-1967 implementation slot (O3: a proxy collection that changes ``ownerOf``).
_ERC1967_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"


def exemption(action: str, params: Optional[Dict[str, Any]], *, autonomous: bool,
              rpc_fn: Optional[Callable[[str], Any]] = None, path: Optional[str] = None,
              now: Optional[float] = None) -> Optional[str]:
    """The reason this act runs with no card under a mandate — one use RESERVED — or None (the
    card stays). Only for an *autonomous* main-agent run the caller vouches for. Never raises:
    any doubt keeps the card."""
    if not autonomous or not action:
        return None
    params = dict(params or {})
    if params.get("dry_run", True) is not False:
        # A quote needs no grant. The hook sees only the params the caller SET, and every
        # money verb's dry_run defaults to True: an absent dry_run is a quote and uses no
        # mandate (a quote-then-buy spent two uses). A verb with no dry_run keeps its card.
        return None
    now = time.time() if now is None else now
    p = path or store_path()
    try:
        with file_lock(p):
            rows = _read(p)
            hit = None
            for m in rows:
                if covers(m, action, params, now) is None and _verified(m):
                    hit = m
                    break
            if hit is None:
                return None
            changed = _facts_changed(hit, rpc_fn)
            if changed:
                logger.warning("mandate %s: %s — the act goes to the card", hit.id, changed)
                return None
            hit.used_count += 1
            from core.wallet.collection_reveal import declared_usd
            hit.used_usd += float(declared_usd(action, params) or 0.0)
            _sign(hit)
            _write(p, rows)
    except Exception as exc:  # noqa: BLE001 — an unreadable store is no mandate
        logger.warning("mandate store unreadable (%s): every act keeps its card", exc)
        return None
    return (f"mandate {hit.id} ({hit.used_count}/{hit.count} acts, "
            f"${hit.used_usd:.2f}/${hit.total_usd:g}) — the owner's standing grant")


def settle_guard(context, decision, *, path: Optional[str] = None) -> None:
    """tx_guard's word on a LIVE transaction of a mandate act (the grant names the mandate).
    The first allowed transaction marks the act as sending: a later refusal keeps the use. A
    refusal before any allowed transaction is sure: nothing was signed, so the reserved use
    (count and declared USD) is given back once. Never raises."""
    try:
        from core.wallet.tx_guard import OWNER_GRANT_KEY
        meta = getattr(context, "metadata", None)
        grant = meta.get(OWNER_GRANT_KEY) if isinstance(meta, dict) else None
        if not isinstance(grant, dict) or not grant.get("mandate") or grant.get("settled"):
            return
        grant["settled"] = "sending" if decision.allowed else "released"
        if not decision.allowed:
            release(str(grant["mandate"]), float(grant.get("max_spend_usd") or 0.0), path=path)
    except Exception as exc:  # noqa: BLE001 — a use not given back is spent, never a second act
        logger.warning("mandate: the use of a refused act was not given back (%s)", exc)


def release(mandate_id: str, usd: float, *, path: Optional[str] = None) -> Optional[Mandate]:
    """Give back ONE reserved use (count and ``usd``) of a mandate whose act the guard refused
    before anything was signed. A mandate that does not verify is left as it is."""
    p = path or store_path()
    with file_lock(p):
        rows = _read(p)
        hit = next((m for m in rows if m.id == str(mandate_id)), None)
        if hit is None or not _verified(hit) or hit.used_count <= 0:
            return None
        hit.used_count -= 1
        hit.used_usd = max(0.0, hit.used_usd - max(0.0, float(usd)))
        _sign(hit)
        _write(p, rows)
    logger.info("mandate %s: a refused act gave its use back (%s/%s)", hit.id,
                hit.used_count, hit.count)
    return hit


def exemption_for_turn(action: str, params: Optional[Dict[str, Any]], context,
                       rpc_fn: Optional[Callable[[str], Any]] = None) -> Optional[str]:
    """:func:`exemption` for the turn in *context*: only the owner's standing run (a recorded cron
    job or goal, ``core.security.owner_turn.is_owner_standing_run`` — every uncertainty answers
    False) may use a mandate; a correspondent, room, leaf or sub-agent turn never does."""
    try:
        # The review (2026-11-20): the owner's STANDING run only — a recorded cron job or goal
        # on the owner tenant with no room, no forged re-entry and no correspondent's message
        # (``is_owner_standing_run``); a merely "autonomous" session is not enough.
        from core.security.owner_turn import is_owner_standing_run
        if not is_owner_standing_run(context):
            return None
    except Exception:  # noqa: BLE001
        return None
    return exemption(action, params, autonomous=True, rpc_fn=rpc_fn)


def status_lines(path: Optional[str] = None, now: Optional[float] = None) -> List[str]:
    now = time.time() if now is None else now
    try:
        rows = mandates(path)
    except Exception as exc:  # noqa: BLE001 — unreadable is not empty
        return [f"mandates: unreadable ({exc}) — every act waits for your card"]
    if not rows:
        return ["mandates: none — every owner-card act waits for your tap"]
    return [m.line(now) for m in rows[-20:]]


__all__ = ["MAX_DAYS", "Mandate", "MandateError", "NFT_ACTS", "covers", "create", "end",
           "exemption", "exemption_for_turn", "mandates", "release", "settle_guard",
           "status_lines", "store_path"]


def _register() -> None:
    """tx_guard calls :func:`settle_guard` after each live decision (a hook in the lower
    module: tx_guard never imports this one, so no import cycle grows)."""
    from core.wallet import tx_guard
    if settle_guard not in tx_guard.AFTER_DECISION_HOOKS:
        tx_guard.AFTER_DECISION_HOOKS.append(settle_guard)


_register()
