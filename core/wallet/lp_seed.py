"""The v4 pool seed declaration (117 §4.1) — core tier.

A v4 pool has no address and no factory record: its identity is the five
PoolKey fields. Creating one, or minting a custom range on one, is therefore
an OWNER decision about a key, a range, two amounts and a price. That decision
is ONE durable row here:

* ``proposed``  — the agent (``lp_add`` with a PoolKey named in full) or the
  owner (``/lp seed …``) wrote it. It grants nothing; it carries a card.
* ``confirmed`` — ONLY the owner seat's ``/lp seed confirm <ref>`` (the card's
  Confirm) moves it here. ``lp_add(protocol='v4', seed=<ref>)`` reads key,
  range, amounts and price from the row and nothing from the caller.
* ``sending``   — claimed for ONE broadcast (a row is one use).
* ``seeded`` / ``failed`` / ``expired`` — terminal.

The price the owner confirmed (``sqrt_price_x96``) is the anchor. The verb may
refuse a pool whose price moved away from it; nothing ever moves the anchor.

The guard reads nothing from this store: every guard rule is on the signed
bytes (``core.wallet.liquidity_guard``), so the signer needs no access to it.

⚠️ A READ never creates the store, and an unreadable store raises
:class:`SeedUnreadable` — it is never "no declaration".
"""
import json
import os
import time
import uuid
from typing import Any, Dict, Optional, Tuple

from core.state.machine import StateMachine

DB_NAME = "lp_seeds.db"
#: A proposal lives as long as its card (``cards.PROPOSAL_TTL_S``).
PROPOSED_TTL_S = 24 * 3600
#: A confirmed declaration is good for one hour, one use (117 §4.1).
CONFIRMED_TTL_S = 3600
GAP_DEFAULT_BPS = 100
GAP_MAX_BPS = 500       # = liquidity_guard.SEED_MAX_GAP_BPS

PROPOSED, CONFIRMED, SENDING = "proposed", "confirmed", "sending"
SEEDED, FAILED, EXPIRED = "seeded", "failed", "expired"

MACHINE = StateMachine(
    name="lp_seed",
    states=frozenset({PROPOSED, CONFIRMED, SENDING, SEEDED, FAILED, EXPIRED}),
    transitions={PROPOSED: {CONFIRMED, EXPIRED},
                 CONFIRMED: {SENDING, EXPIRED},
                 # a refusal before the broadcast, or a revert, moved nothing: the
                 # declaration is whole again until it expires
                 SENDING: {SEEDED, FAILED, CONFIRMED}},
    terminal=frozenset({SEEDED, FAILED, EXPIRED}),
    initial=frozenset({PROPOSED}),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lp_seeds (
    ref          TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    state        TEXT NOT NULL,
    fields       TEXT NOT NULL,
    origin       TEXT NOT NULL,
    created_at   REAL NOT NULL,
    confirmed_at REAL,
    finished_at  REAL,
    token_id     TEXT,
    tx_hash      TEXT,
    note         TEXT
);
"""

#: Every field a declaration stores. Amounts and the price are decimal strings
#: (they pass uint128 / uint160; JSON numbers would lose them).
FIELDS = ("chain", "currency0", "currency1", "fee", "tick_spacing", "hooks", "pool_id",
          "tick_lower", "tick_upper", "amount0_raw", "amount1_raw", "dec0", "dec1",
          "sym0", "sym1", "sqrt_price_x96", "price_source", "gap_bps")

PRICE_SOURCES = ("owner", "agent")
_ZERO = "0x" + "0" * 40


class SeedError(ValueError):
    """The declaration is refused, missing, used or expired."""


class SeedUnreadable(RuntimeError):
    """The store could not be read. Never the same as "no declaration"."""


def db_path(data_dir: Optional[str] = None) -> str:
    from core.runtime_paths import data_home_db_path
    return data_home_db_path(DB_NAME, data_dir=data_dir)


def init_schema(path: Optional[str] = None) -> str:
    """Create the store (the WRITE path only). Returns the path."""
    from core.sqlite_util import init_schema as _init
    p = path or db_path()
    _init(p, _SCHEMA, mkdir=True)
    return p


# -- the rules of a declaration (pure) ------------------------------------------------

def _addr(value: Any, name: str) -> str:
    text = str(value or "").lower()
    if len(text) != 42 or not text.startswith("0x"):
        raise SeedError(f"{name} is not an address")
    try:
        bytes.fromhex(text[2:])
    except ValueError:
        raise SeedError(f"{name} is not an address") from None
    return text


def _int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise SeedError(f"{name} must be a whole number")
    try:
        return int(str(value), 10)
    except (TypeError, ValueError):
        raise SeedError(f"{name} must be a whole number") from None


def validate(fields: Dict[str, Any]) -> Dict[str, Any]:
    """The declaration as stored, or :class:`SeedError`. The same limits the
    guard holds the signed bytes to (117 §4.1, §4.4), so a card is never made
    for a shape the guard refuses."""
    from core.wallet import dex_registry, liquidity_guard as G, univ3_math as M
    unknown = sorted(set(fields) - set(FIELDS))
    if unknown:
        raise SeedError(f"unknown declaration field(s): {', '.join(unknown)}")
    chain = str(fields.get("chain") or "").lower()
    row = dex_registry.row_for(chain, "v4")
    if row is None or not row.position_manager:
        raise SeedError(f"chain {chain!r} has no pinned Uniswap v4 deployment")
    c0, c1 = _addr(fields.get("currency0"), "currency0"), _addr(fields.get("currency1"), "currency1")
    if int(c0, 16) >= int(c1, 16):
        raise SeedError("the two currencies must be different and sorted (currency0 < currency1)")
    fee, spacing = _int(fields.get("fee"), "fee"), _int(fields.get("tick_spacing"), "tick_spacing")
    if fee & G.V4_DYNAMIC_FEE_FLAG or not 0 <= fee <= G.V4_MAX_LP_FEE:
        raise SeedError("fee must be a static fee in millionths (0..1000000); a dynamic fee is refused")
    if not 1 <= spacing <= 32767:
        raise SeedError("tick_spacing must be 1..32767")
    hooks = _addr(fields.get("hooks") or _ZERO, "hooks")
    if not dex_registry.hook_pinned(chain, hooks):
        raise SeedError(f"hook {hooks} has no pinned code hash on {chain}; a pool with an "
                        f"unknown hook is refused")
    lo, hi = _int(fields.get("tick_lower"), "tick_lower"), _int(fields.get("tick_upper"), "tick_upper")
    if not M.MIN_TICK <= lo < hi <= M.MAX_TICK or lo % spacing or hi % spacing:
        raise SeedError(f"ticks [{lo},{hi}] must be ordered, in bounds and multiples of {spacing}")
    a0, a1 = _int(fields.get("amount0_raw"), "amount0_raw"), _int(fields.get("amount1_raw"), "amount1_raw")
    if a0 < 0 or a1 < 0 or not (a0 or a1) or max(a0, a1) >= 2 ** 128:
        raise SeedError("the amounts must be positive and fit uint128")
    dec0, dec1 = _int(fields.get("dec0"), "dec0"), _int(fields.get("dec1"), "dec1")
    if not (0 <= dec0 <= 36 and 0 <= dec1 <= 36):
        raise SeedError("token decimals are outside the supported range 0..36")
    sqrt = _int(fields.get("sqrt_price_x96"), "sqrt_price_x96")
    if not M.MIN_SQRT_RATIO <= sqrt < M.MAX_SQRT_RATIO:
        raise SeedError("the price is outside what a v4 pool can hold")
    liq = M.liquidity_for_amounts(sqrt, M.sqrt_price_at_tick(lo), M.sqrt_price_at_tick(hi), a0, a1)
    if not 0 < liq < 2 ** 128:
        raise SeedError("at this price and range the two amounts make no liquidity (a range "
                        "above the price takes only currency0, below it only currency1)")
    gap = _int(fields.get("gap_bps", GAP_DEFAULT_BPS), "gap_bps")
    if not 1 <= gap <= GAP_MAX_BPS:
        raise SeedError(f"gap_bps must be 1..{GAP_MAX_BPS}")
    source = str(fields.get("price_source") or "")
    if source not in PRICE_SOURCES:
        raise SeedError(f"price_source must be one of {', '.join(PRICE_SOURCES)}")
    pid = G.v4_pool_id((c0, c1, fee, spacing, hooks))
    given = str(fields.get("pool_id") or pid).lower()
    if given != pid:
        raise SeedError("pool_id is not the hash of the five PoolKey fields")
    sym = lambda v: "".join(ch for ch in str(v or "?") if ch.isalnum() or ch in "._-$")[:16] or "?"
    return {"chain": chain, "currency0": c0, "currency1": c1, "fee": fee,
            "tick_spacing": spacing, "hooks": hooks, "pool_id": pid, "tick_lower": lo,
            "tick_upper": hi, "amount0_raw": str(a0), "amount1_raw": str(a1), "dec0": dec0,
            "dec1": dec1, "sym0": sym(fields.get("sym0")), "sym1": sym(fields.get("sym1")),
            "sqrt_price_x96": str(sqrt), "price_source": source, "gap_bps": gap}


def key_of(fields: Dict[str, Any]) -> Dict[str, Any]:
    return {"currency0": fields["currency0"], "currency1": fields["currency1"],
            "fee": int(fields["fee"]), "tickSpacing": int(fields["tick_spacing"]),
            "hooks": fields["hooks"]}


# -- the store ------------------------------------------------------------------------

def propose(user_id: str, fields: Dict[str, Any], *, origin: str, path: Optional[str] = None,
            now: Optional[float] = None) -> Tuple[str, Dict[str, Any]]:
    """Validate and store a PROPOSED declaration. Returns ``(ref, fields)``."""
    clean = validate(dict(fields))
    ref = f"s{uuid.uuid4().hex[:10]}"
    from core.sqlite_util import execute_retry
    p = init_schema(path)
    execute_retry(p, "INSERT INTO lp_seeds (ref, user_id, state, fields, origin, created_at) "
                  "VALUES (?,?,?,?,?,?)",
                  (ref, str(user_id), PROPOSED, json.dumps(clean), str(origin),
                   time.time() if now is None else float(now)))
    return ref, clean


def _row(ref: str, path: Optional[str]) -> Optional[Dict[str, Any]]:
    p = path or db_path()
    try:
        os.stat(p)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SeedUnreadable(f"the seed declaration store is unreachable: {exc}") from exc
    try:
        from core.sqlite_util import execute_retry
        have = execute_retry(p, "SELECT 1 FROM sqlite_master WHERE type='table' AND "
                             "name='lp_seeds'", (), fetch="one")
        r = execute_retry(p, "SELECT * FROM lp_seeds WHERE ref=?", (str(ref),),
                          fetch="one") if have else None
        return None if r is None else {**dict(r), "fields": json.loads(r["fields"] or "{}")}
    except Exception as exc:
        raise SeedUnreadable(f"the seed declaration store is unreadable: {exc}") from exc


def _move(ref: str, to: str, froms, path: Optional[str], **cols) -> bool:
    from core.state.sql import cas_transition
    return cas_transition(path or db_path(), "lp_seeds", key="ref=?", key_params=(str(ref),),
                          machine=MACHINE, to=to, from_states=froms, set_cols=cols,
                          status_col="state")


def get(ref: str, *, user_id: str, path: Optional[str] = None,
        now: Optional[float] = None) -> Dict[str, Any]:
    """The row of *ref* for this user, with an overdue one moved to ``expired``.
    Raises :class:`SeedError` when there is none, :class:`SeedUnreadable` when
    the store cannot be read."""
    r = _row(ref, path)
    if r is None or str(r["user_id"]) != str(user_id):
        raise SeedError(f"no seed declaration {ref!r} — propose one with lp_add(protocol='v4', "
                        f"fee, tick_spacing, range, initial_price) or /lp seed")
    ts = time.time() if now is None else float(now)
    overdue = ((r["state"] == PROPOSED and ts - float(r["created_at"]) > PROPOSED_TTL_S)
               or (r["state"] == CONFIRMED
                   and ts - float(r["confirmed_at"] or 0) > CONFIRMED_TTL_S))
    if overdue and _move(ref, EXPIRED, (r["state"],), path, finished_at=ts):
        r["state"] = EXPIRED
    return r


def confirm(ref: str, *, user_id: str, path: Optional[str] = None,
            now: Optional[float] = None) -> Dict[str, Any]:
    """The OWNER SEAT's write: ``proposed -> confirmed``. Raises :class:`SeedError`
    for any other state."""
    r = get(ref, user_id=user_id, path=path, now=now)
    ts = time.time() if now is None else float(now)
    if r["state"] != PROPOSED or not _move(ref, CONFIRMED, (PROPOSED,), path, confirmed_at=ts):
        raise SeedError(f"seed declaration {ref} is {r['state'].upper()}, not open for a confirm")
    return {**r, "state": CONFIRMED, "confirmed_at": ts}


def claim(ref: str, *, path: Optional[str] = None) -> bool:
    """``confirmed -> sending``: the one use, taken just before the signature."""
    return _move(ref, SENDING, (CONFIRMED,), path)


def release(ref: str, *, note: str = "", path: Optional[str] = None) -> bool:
    """``sending -> confirmed``: nothing was broadcast, or the transaction reverted."""
    return _move(ref, CONFIRMED, (SENDING,), path, note=str(note)[:500])


def finish(ref: str, state: str, *, token_id: Any = None, tx_hash: str = "", note: str = "",
           path: Optional[str] = None, now: Optional[float] = None) -> bool:
    """``sending -> seeded | failed``."""
    if state not in (SEEDED, FAILED):
        raise ValueError(f"a seed finishes as seeded or failed, not {state!r}")
    return _move(ref, state, (SENDING,), path,
                 finished_at=time.time() if now is None else float(now),
                 token_id=None if token_id is None else str(token_id),
                 tx_hash=str(tx_hash or ""), note=str(note)[:500])


# -- what the owner reads -------------------------------------------------------------

def _human(raw: Any, dec: int) -> str:
    from decimal import Decimal
    return f"{Decimal(int(raw)) / (Decimal(10) ** int(dec)):,.8f}".rstrip("0").rstrip(".")


def price_of(sqrt_price_x96: Any, dec0: int, dec1: int) -> float:
    """currency1 per currency0, in whole-token units."""
    s = int(sqrt_price_x96) / 2 ** 96
    return s * s * 10 ** int(dec0) / 10 ** int(dec1)


def summary_lines(f: Dict[str, Any]) -> list:
    """The declaration in words. Every number comes from the validated row."""
    from core.wallet import univ3_math as M
    p = price_of(f["sqrt_price_x96"], f["dec0"], f["dec1"])
    lo = M.tick_to_price(f["tick_lower"], f["dec0"], f["dec1"])
    hi = M.tick_to_price(f["tick_upper"], f["dec0"], f["dec1"])
    native = lambda c, s: f"{s} (native)" if c == _ZERO else f"{s} {c}"
    who = ("a number you typed" if f["price_source"] == "owner"
           else "a number the AGENT proposed — check it; no market set it")
    return [
        f"Uniswap v4 pool on {f['chain']}: {native(f['currency0'], f['sym0'])} / "
        f"{native(f['currency1'], f['sym1'])}",
        f"fee {f['fee'] / 10000:g}%, tick spacing {f['tick_spacing']}, "
        + ("no hook" if f["hooks"] == _ZERO else f"hook {f['hooks']}"),
        f"pool id {f['pool_id']}",
        f"price: 1 {f['sym0']} = {p:.10g} {f['sym1']} (1 {f['sym1']} = {1 / p:.10g} "
        f"{f['sym0']}) — {who}",
        f"range: {lo:.6g} to {hi:.6g} {f['sym1']} per {f['sym0']} (ticks {f['tick_lower']} "
        f"to {f['tick_upper']})",
        f"at most {_human(f['amount0_raw'], f['dec0'])} {f['sym0']} and "
        f"{_human(f['amount1_raw'], f['dec1'])} {f['sym1']}; the position goes to the wallet",
        f"allowed price gap: {f['gap_bps'] / 100:g}%",
    ]


def card_body(f: Dict[str, Any], *, notes: Tuple[str, ...] = ()) -> str:
    lines = summary_lines(f) + [n for n in notes if n]
    lines += ["",
              "Confirm declares this pool, range, price and both amounts for ONE use within "
              "one hour. It sends nothing and it raises no limit: the wallet's own caps and "
              "LP_ETH_CAP still judge the transaction."]
    return "\n".join(lines)


def seed_card(user_id: str, ref: str, f: Dict[str, Any], *, origin: str,
              session_id: Optional[str] = None, notes: Tuple[str, ...] = (), st: Any = None):
    """The ONE owner card of a proposed declaration; Confirm runs
    ``/lp seed confirm <ref>`` on the owner's seat."""
    from core.surfaces import cards
    return cards.job_card(user_id, ["/lp", "seed", "confirm", ref],
                          title="Create a pool position — confirm the declaration",
                          body=card_body(f, notes=notes), session_id=session_id, st=st,
                          origin=cards.ORIGIN_AGENT if origin == "agent" else cards.ORIGIN_SYSTEM)


__all__ = ["CONFIRMED", "CONFIRMED_TTL_S", "DB_NAME", "EXPIRED", "FAILED", "FIELDS",
           "GAP_DEFAULT_BPS", "GAP_MAX_BPS", "MACHINE", "PROPOSED", "PROPOSED_TTL_S", "SEEDED",
           "SENDING", "SeedError", "SeedUnreadable", "card_body", "claim", "confirm", "db_path",
           "finish", "get", "init_schema", "key_of", "price_of", "propose", "release",
           "seed_card", "summary_lines", "validate"]
