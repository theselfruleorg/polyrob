"""The income allocation rule — P % of income to a buy of a pinned token (proposal 122 W4).

ONE rule: ``on income of kinds K, P % to a buy of a pinned token``, the token held in the
treasury. The owner sets it from the owner seat (``/wallet income …``, a quote and then ``go``);
the agent has no verb that writes it, and :func:`set_rule` / :func:`end_rule` refuse every
context that is not an owner-seat call. The rule is signed by the wallet key
(``core/wallet/owner_signed.py``, the mandate's pattern too): a rule a shell wrote, or one whose percent was
changed, does not verify and allocates nothing.

The kinds (decision D2: all four count):

* ``fees``     — launch fee claims (the ``launchpad_claim`` row of the spend audit, its
  ``income_raw``);
* ``nft``      — NFT sales (the holdings watch's ``Sold`` facts);
* ``payments`` — offer and x402 payments (a completed invoice);
* ``trading``  — realized trading profit (the position book's realized rows, every holder:
  income inside an NFT account the treasury holds counts too, 124 M2/D6).

It adds NO second ledger: ``tools/income_rule/sources.py`` reads those records and this module
keeps only what the rule needs — which income it already counted, the debt it owes, and the
buys it ran. Debt first, buy second:

* each income event adds P % of its USD value to the "owed to the rule" balance (an
  ``entries`` row that names the income);
* one buy runs when the balance passes the rule's minimum, or once a day for any balance of
  at least :data:`FLOOR_USD`;
* a buy holds the entries it pays (``held``); a done buy marks them ``paid`` and the record
  shows which income each buy came from; a failed buy gives them back (``owed``) and says so —
  a debt is never dropped in silence;
* an event that cannot be valued is retried for a day, then kept as ``unvalued`` (shown, not
  counted).

The state: ``<wallet data dir>/income_rule.json`` (atomic writes under a file lock). An
unreadable file raises — it is never read as "no rule" or "no debt". A rollback
(``polyrob update rollback``) keeps the live file (``core.update_snapshot.context.
ROLLBACK_KEPT_FILES``): a restore never revives an ended rule, re-counts an income (``seen``)
or makes a paid debt owed again.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from core.state.files import atomic_write_json, file_lock
from core.wallet import owner_signed

logger = logging.getLogger(__name__)

STORE_NAME = "income_rule.json"
#: The income kinds and the words the owner reads.
KINDS: Dict[str, str] = {
    "fees": "launch fee claims",
    "nft": "NFT sales",
    "payments": "offer and x402 payments",
    "trading": "realized trading profit",
}
DEFAULT_MIN_USD = 10.0
#: The once-a-day buy runs only for a balance of at least this (a smaller buy costs more gas
#: than it buys); it waits for more income.
FLOOR_USD = 1.0
DAY_SEC = 86_400
#: After a failed buy, the next try waits this long.
RETRY_SEC = 6 * 3600
#: An income event with no USD value is retried this long, then kept as ``unvalued``.
UNVALUED_GIVE_UP_SEC = DAY_SEC
KEEP_PAID = 200
KEEP_BUYS = 100
KEEP_ENDED = 10

OWED, HELD, PAID, UNVALUED = "owed", "held", "paid", "unvalued"
B_RUNNING, B_QUEUED, B_DONE, B_FAILED, B_UNKNOWN = (
    "running", "queued", "done", "failed", "unknown")
#: What the wallet signs: this prefix and the sha256 of the rule.
SIG_PREFIX = b"polyrob owner income rule v1:"


class RuleError(ValueError):
    """The rule cannot be set as asked; the text says why."""


@dataclass
class Rule:
    id: str
    created_at: float
    kinds: List[str]
    percent: float
    chain: str
    token: str
    symbol: str = ""
    min_usd: float = DEFAULT_MIN_USD
    ended_at: Optional[float] = None
    sig: str = ""

    def line(self) -> str:
        kinds = ", ".join(KINDS[k] for k in self.kinds if k in KINDS)
        name = f"{self.symbol} " if self.symbol else ""
        return (f"{self.percent:g} % of {kinds} buys {name}{self.token} on {self.chain} "
                f"(held in the treasury); a buy when ${self.min_usd:g} is owed, or once a day")


@dataclass
class IncomeEvent:
    """One income, as a reader of an existing record found it. ``usd`` None = not valued."""
    key: str
    kind: str
    at: float
    usd: Optional[float]
    detail: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------- the store

def store_path(data_home: Optional[str] = None) -> str:
    if data_home:
        return os.path.join(str(data_home), "wallet", STORE_NAME)
    from core.wallet.audit_sink import _wallet_data_dir
    return os.path.join(_wallet_data_dir(), STORE_NAME)


def empty_state() -> Dict[str, Any]:
    return {"version": 1, "rule": None, "ended": [], "seen": {}, "trading_hw": {},
            "unvalued": {}, "entries": [], "buys": [], "last_buy_at": 0.0}


def load(path: str) -> Dict[str, Any]:
    """The state; no file = the empty state. An unreadable file RAISES."""
    if not os.path.exists(path):
        return empty_state()
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ValueError(f"{path} is not an income rule store")
    base = empty_state()
    base.update(raw)
    return base


def save(path: str, state: Dict[str, Any]) -> None:
    state["entries"] = _trim_paid(state.get("entries") or [])
    state["buys"] = _trim_buys(state.get("buys") or [])
    state["ended"] = list(state.get("ended") or [])[-KEEP_ENDED:]
    atomic_write_json(path, state, indent=1)


def _trim_buys(buys: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The last :data:`KEEP_BUYS` finished buys, and EVERY open one: a running, queued or
    unknown buy is never trimmed (its held entries name it; ``settle_unknown`` needs it)."""
    buys = list(buys)
    done = [b for b in buys if b.get("state") not in (B_RUNNING, B_QUEUED, B_UNKNOWN)]
    if len(done) <= KEEP_BUYS:
        return buys
    drop = {id(b) for b in done[:len(done) - KEEP_BUYS]}
    return [b for b in buys if id(b) not in drop]


def _trim_paid(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    paid = [e for e in entries if e.get("state") == PAID]
    if len(paid) <= KEEP_PAID:
        return entries
    drop = {id(e) for e in paid[:len(paid) - KEEP_PAID]}
    return [e for e in entries if id(e) not in drop]


# ---------------------------------------------------------------------------- the signature

#: The wallet key that signs the rule (``core.wallet.owner_signed``); tests replace it.
_signer = owner_signed.wallet_signer


def _sign(rule: Rule) -> None:
    owner_signed.sign(rule, SIG_PREFIX, _signer(), RuleError, noun="the rule",
                      remote="the income rule cannot be set yet")


def _verified(rule: Rule) -> bool:
    return owner_signed.verified(rule, SIG_PREFIX, _signer())


def rule_of(state: Dict[str, Any]) -> Optional[Rule]:
    """The live rule, or None: no rule, an ended one, or one that does not verify (logged)."""
    raw = state.get("rule")
    if not isinstance(raw, dict):
        return None
    try:
        rule = Rule(**raw)
    except TypeError:
        logger.warning("income rule: the stored rule has an unknown shape; it allocates nothing")
        return None
    if rule.ended_at is not None:
        return None
    if not _verified(rule):
        logger.warning("income rule %s does not verify; it allocates nothing", rule.id)
        return None
    return rule


# ---------------------------------------------------------------------------- the owner's acts

def _owner_refusal(ctx: Any, verb: str) -> Optional[str]:
    return owner_signed.seat_refusal(
        ctx, verb, does="sets how income is spent", public="set how income is spent",
        denied="only the owner seat sets the income rule (the agent cannot)")


def parse_kinds(text: str) -> List[str]:
    """``all`` or a comma list of :data:`KINDS`. Raises :class:`RuleError`."""
    words = [w.strip().lower() for w in str(text or "").split(",") if w.strip()]
    if words == ["all"]:
        return list(KINDS)
    bad = [w for w in words if w not in KINDS]
    if not words or bad:
        raise RuleError(f"the kinds are `all` or a comma list of {', '.join(KINDS)}; "
                        f"got {text!r}")
    return [k for k in KINDS if k in words]


def check_target(chain: str, token: str, *, pins_db: Optional[str] = None) -> Dict[str, str]:
    """The owner's pin row of *token* on *chain*. Raises :class:`RuleError` when it is not
    pinned (the rule buys only a token the owner pinned)."""
    from core.wallet import token_pins
    chain = token_pins.norm_chain(chain)
    try:
        row = token_pins.owner_pin(chain, token, db_path=pins_db, strict=True)
    except Exception as exc:  # noqa: BLE001 — an unreadable pin store is no pin
        raise RuleError(f"the token pins could not be read ({exc})") from exc
    if row is None:
        raise RuleError(f"{token} is not a pinned token on {chain} — pin it first "
                        f"(/wallet trust {chain} {token} <SYMBOL>)")
    return {"chain": chain, "address": str(row["address"]), "symbol": str(row.get("symbol") or "")}


def set_rule(ctx: Any, *, kinds: Iterable[str], percent: float, chain: str, token: str,
             min_usd: float = DEFAULT_MIN_USD, path: Optional[str] = None,
             now: Optional[float] = None, pins_db: Optional[str] = None,
             realized: Optional[Dict[str, float]] = None) -> Rule:
    """Set THE rule (the owner seat only); a rule already set ends. The debt stays: the new
    rule pays it. *realized* is the trading baseline (the realized totals now, so profit
    realized before the rule never counts). Raises :class:`RuleError`."""
    refusal = _owner_refusal(ctx, "set the income rule")
    if refusal:
        raise RuleError(refusal)
    now = time.time() if now is None else now
    kinds = [k for k in KINDS if k in set(kinds)]
    if not kinds:
        raise RuleError("the rule names at least one income kind")
    percent = float(percent)
    if not 0 < percent <= 100:
        raise RuleError(f"the percent is more than 0 and at most 100, not {percent:g}")
    if float(min_usd) < FLOOR_USD:
        raise RuleError(f"the minimum buy is at least ${FLOOR_USD:g}")
    if "trading" in kinds and realized is None:
        raise RuleError("the realized trading totals could not be read, so profit made before "
                        "the rule cannot be told apart; try again")
    target = check_target(chain, token, pins_db=pins_db)
    rule = Rule(id="r_" + secrets.token_hex(4), created_at=now, kinds=kinds, percent=percent,
                chain=target["chain"], token=target["address"], symbol=target["symbol"],
                min_usd=float(min_usd))
    _sign(rule)
    p = path or store_path()
    with file_lock(p):
        state = load(p)
        _end_current(state, now)
        state["rule"] = asdict(rule)
        if realized is not None:
            # Profit realized before the rule never counts: the mark is the total NOW.
            state["trading_hw"] = {k: float(v) for k, v in realized.items()}
        save(p, state)
    logger.info("income rule %s set: %s", rule.id, rule.line())
    return rule


def _end_current(state: Dict[str, Any], now: float) -> None:
    old = state.get("rule")
    if isinstance(old, dict):
        old = dict(old, ended_at=old.get("ended_at") or now)
        state.setdefault("ended", []).append(old)
    state["rule"] = None


def end_rule(ctx: Any, *, path: Optional[str] = None, now: Optional[float] = None) -> Optional[Dict]:
    """End the rule (the owner seat only). The debt stays, shown, until a rule pays it."""
    refusal = _owner_refusal(ctx, "end the income rule")
    if refusal:
        raise RuleError(refusal)
    now = time.time() if now is None else now
    p = path or store_path()
    with file_lock(p):
        state = load(p)
        old = state.get("rule")
        if not isinstance(old, dict):
            return None
        _end_current(state, now)
        save(p, state)
    return state["ended"][-1]


# ---------------------------------------------------------------------------- the debt

def trading_events(state: Dict[str, Any], totals: Dict[str, Dict[str, Any]],
                   now: float) -> List[IncomeEvent]:
    """Realized-profit events from the realized totals (``{key: {"usd", "label"}}``): the
    growth of each total above its high-water mark. A loss lowers nothing (the high-water mark
    stays), so profit counts only once it passes what was counted before. A key with no mark
    is a holding first realized after the rule was set (:func:`set_rule` marks every total of
    that moment): its whole total counts."""
    hw = state.setdefault("trading_hw", {})
    out = []
    for key, row in sorted(totals.items()):
        total = float(row.get("usd") or 0.0)
        if key not in hw:
            hw[key] = 0.0
        gain = total - float(hw[key])
        if gain > 0:
            out.append(IncomeEvent(key=f"trading:{key}:{total:.6f}", kind="trading", at=now,
                                   usd=gain, detail=f"realized profit {row.get('label') or key}"))
            hw[key] = total
    return out


def accrue(state: Dict[str, Any], rule: Rule, events: Iterable[IncomeEvent],
           now: float) -> List[Dict[str, Any]]:
    """Add P % of each new event to the debt. Returns the new entries (and the ``unvalued``
    rows given up on). An event is counted once (``seen``)."""
    seen = state.setdefault("seen", {})
    unvalued = state.setdefault("unvalued", {})
    entries = state.setdefault("entries", [])
    added = []
    for ev in events:
        if ev.key in seen:
            continue
        if ev.at < rule.created_at or ev.kind not in rule.kinds:
            seen[ev.key] = now
            continue
        if ev.usd is None:
            first = float((unvalued.get(ev.key) or {}).get("first") or now)
            if now - first < UNVALUED_GIVE_UP_SEC:
                unvalued[ev.key] = {"first": first, "kind": ev.kind, "detail": ev.detail}
                continue
            unvalued.pop(ev.key, None)
            seen[ev.key] = now
            row = {"key": ev.key, "kind": ev.kind, "at": ev.at, "income_usd": None,
                   "owed_usd": 0.0, "percent": rule.percent, "detail": ev.detail,
                   "state": UNVALUED, "buy": None, "rule": rule.id}
            entries.append(row)
            added.append(row)
            continue
        unvalued.pop(ev.key, None)
        seen[ev.key] = now
        if ev.usd <= 0:
            continue
        row = {"key": ev.key, "kind": ev.kind, "at": ev.at, "income_usd": round(ev.usd, 6),
               "owed_usd": round(ev.usd * rule.percent / 100.0, 6), "percent": rule.percent,
               "detail": ev.detail, "state": OWED, "buy": None, "rule": rule.id}
        entries.append(row)
        added.append(row)
    return added


def owed_usd(state: Dict[str, Any]) -> float:
    return round(sum(float(e.get("owed_usd") or 0) for e in state.get("entries") or ()
                     if e.get("state") == OWED), 6)


def open_buy(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The buy in flight (running, or queued on the owner's decision), or None. A buy of
    unknown end does not block the next one: its entries stay held, so nothing is paid twice."""
    for b in reversed(state.get("buys") or []):
        if b.get("state") in (B_RUNNING, B_QUEUED):
            return b
    return None


def due(state: Dict[str, Any], rule: Rule, now: float) -> Optional[str]:
    """Why a buy runs now, or None."""
    if open_buy(state) is not None:
        return None
    owed = owed_usd(state)
    if owed < FLOOR_USD:
        return None
    last = state.get("buys") or []
    if last and last[-1].get("state") == B_FAILED and \
            now - float(state.get("last_buy_at") or 0) < RETRY_SEC:
        return None
    if owed >= rule.min_usd:
        return f"${owed:.2f} owed passed the ${rule.min_usd:g} minimum"
    oldest = min(float(e.get("at") or now) for e in state["entries"] if e.get("state") == OWED)
    since = max(oldest, float(state.get("last_buy_at") or 0))
    if now - since >= DAY_SEC:
        return f"${owed:.2f} owed for a day"
    return None


def start_buy(state: Dict[str, Any], rule: Rule, now: float) -> Dict[str, Any]:
    """Hold every owed entry for one new buy (``running``)."""
    bid = "b_" + secrets.token_hex(4)
    keys, usd = [], 0.0
    for e in state.get("entries") or ():
        if e.get("state") == OWED:
            e["state"], e["buy"] = HELD, bid
            keys.append(e["key"])
            usd += float(e.get("owed_usd") or 0)
    buy = {"id": bid, "at": now, "rule": rule.id, "usd": round(usd, 6), "chain": rule.chain,
           "token": rule.token, "state": B_RUNNING, "from": keys, "text": "", "tx": "",
           "card": ""}
    state.setdefault("buys", []).append(buy)
    state["last_buy_at"] = now
    return buy


def _buy(state: Dict[str, Any], buy_id: str) -> Dict[str, Any]:
    for b in state.get("buys") or ():
        if b.get("id") == buy_id:
            return b
    raise KeyError(buy_id)


def finish_buy(state: Dict[str, Any], buy_id: str, *, ok: bool, text: str = "",
               tx: str = "", now: Optional[float] = None) -> Dict[str, Any]:
    """A done buy pays its entries; a failed one gives them back to the debt."""
    buy = _buy(state, buy_id)
    buy.update(state=B_DONE if ok else B_FAILED, text=str(text or "")[:1500],
               tx=str(tx or ""), ended_at=time.time() if now is None else now)
    for e in state.get("entries") or ():
        if e.get("buy") == buy_id and e.get("state") == HELD:
            if ok:
                e["state"] = PAID
            else:
                e["state"], e["buy"] = OWED, None
    return buy


def queue_buy(state: Dict[str, Any], buy_id: str, card_id: str, text: str) -> Dict[str, Any]:
    """The buy waits for the owner's decision (its entries stay held)."""
    buy = _buy(state, buy_id)
    buy.update(state=B_QUEUED, card=str(card_id), text=str(text or "")[:1500])
    return buy


def mark_unknown(state: Dict[str, Any], buy_id: str, text: str, *,
                 tx: str = "") -> Dict[str, Any]:
    """A buy whose end is unknown keeps its entries held: giving them back could buy twice."""
    buy = _buy(state, buy_id)
    buy.update(state=B_UNKNOWN, text=str(text or "")[:1500])
    if tx:
        buy["tx"] = str(tx)
    return buy


def settle_unknown(ctx: Any, buy_id: str, *, landed: bool, path: Optional[str] = None,
                   now: Optional[float] = None) -> Dict[str, Any]:
    """The owner's word on a buy of unknown end (the owner seat only): ``landed`` pays its
    entries; not landed gives them back to the debt."""
    refusal = _owner_refusal(ctx, "settle an income rule buy")
    if refusal:
        raise RuleError(refusal)
    p = path or store_path()
    with file_lock(p):
        state = load(p)
        try:
            buy = _buy(state, str(buy_id).strip())
        except KeyError:
            raise RuleError(f"no buy {buy_id!r}") from None
        if buy.get("state") != B_UNKNOWN:
            raise RuleError(f"buy {buy['id']} is {buy['state']}, not of unknown end")
        buy = finish_buy(state, buy["id"], ok=landed, now=now,
                         text=("the owner said it landed" if landed
                               else "the owner said it did not land; the debt is owed again"))
        save(p, state)
    return buy


# ---------------------------------------------------------------------------- the view

def buy_line(buy: Dict[str, Any]) -> str:
    tx = f", tx {buy['tx']}" if buy.get("tx") else ""
    card = f", decision {buy['card']}" if buy.get("card") else ""
    return (f"{buy['id']} [{buy['state']}] ${float(buy.get('usd') or 0):.2f} from "
            f"{len(buy.get('from') or [])} income{tx}{card}")


def status_lines(state: Dict[str, Any], *, rule: Optional[Rule] = None,
                 limit: int = 5) -> List[str]:
    out = [f"Rule: {rule.id} — {rule.line()}" if rule else "Rule: none set"]
    out.append(f"Owed to the rule: ${owed_usd(state):.2f}")
    held = open_buy(state)
    if held:
        out.append(f"In flight: {buy_line(held)}")
    for b in state.get("buys") or ():
        if b.get("state") == B_UNKNOWN:
            out.append(f"End unknown: {buy_line(b)} — check the transaction, then "
                       f"/wallet income settle {b['id']} landed|lost go")
    unval = [e for e in state.get("entries") or () if e.get("state") == UNVALUED]
    if state.get("unvalued") or unval:
        out.append(f"Not valued: {len(state.get('unvalued') or {})} waiting, {len(unval)} given "
                   f"up (shown, not counted)")
    buys = list(state.get("buys") or [])[-limit:]
    if buys:
        out.append("Last buys:")
        by_key = {e["key"]: e for e in state.get("entries") or ()}
        for b in reversed(buys):
            out.append(f"  {buy_line(b)}")
            for k in (b.get("from") or [])[:5]:
                e = by_key.get(k)
                if e:
                    out.append(f"    · {e['detail']} — ${float(e.get('income_usd') or 0):.2f} "
                               f"income, ${float(e.get('owed_usd') or 0):.2f} to the rule")
            if b.get("state") == B_FAILED and b.get("text"):
                out.append(f"    failed: {str(b['text'])[:200]} — the debt stays")
    return out


__all__ = ["KINDS", "IncomeEvent", "Rule", "RuleError", "accrue", "due", "end_rule",
           "finish_buy", "load", "mark_unknown", "open_buy", "owed_usd", "parse_kinds",
           "queue_buy", "rule_of", "save", "set_rule", "settle_unknown", "start_buy", "status_lines",
           "store_path", "trading_events"]
