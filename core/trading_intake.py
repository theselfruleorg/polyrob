"""The trading mandate's intake card (086 W5, §3.1) — core tier.

"I want to trade tokens" ends in ONE owner card. Two writers make it:

* the owner seat — ``/trade start`` with or without fields (``surfaces/telegram/
  trade_ops.py``; the REPL calls the same code);
* the agent — the ``trade_mandate_propose`` action (``tools/controller/
  trade_mandate_action.py``) after the ``trading-mandate`` skill's intake.

Both go through :func:`intake_card`: the fields are validated by the mandate's
OWN rules (``core.trading_mandate.draft``), stored as a DRAFT row
(``mandate_drafts`` in ``trading.db``) and shown as the echo line of §3.1 —
every number on the card comes from the validated draft and the preset
(``core.trading_presets``), never from text the model wrote. The card is a
``KIND_JOB`` card whose Confirm runs ONE seat line, ``/trade start <ref>``: the
seat reads the draft, creates the mandate with ``actor="owner_seat"`` and its jobs.
A draft writes nothing else and grants nothing; it expires with its card.

⚠️ A READ never creates the store; an unreadable store raises
``core.trading_mandate.MandateUnreadable`` (never "no draft").

Tier-0: imports nothing above ``core``.
"""
import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.trading_mandate import Mandate, MandateError, MandateUnreadable

#: A draft lives as long as the card that carries it (``cards.PROPOSAL_TTL_S``).
DRAFT_TTL_S = 24 * 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mandate_drafts (
    ref        TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    fields     TEXT NOT NULL,
    origin     TEXT NOT NULL,
    created_at REAL NOT NULL,
    used_at    REAL,
    mandate_id TEXT
);
"""

#: The fields a draft may carry (``core.trading_mandate.draft`` keywords).
FIELDS = ("budget_usd", "chains", "preset", "ticket_usd", "slots", "breakers")


def _path(path: Optional[str]) -> str:
    from core.trading_mandate import db_path
    return path or db_path()


def _validated(user_id: str, fields: Dict[str, Any]) -> Mandate:
    from core.trading_mandate import draft
    unknown = sorted(set(fields) - set(FIELDS))
    if unknown:
        raise MandateError(f"unknown mandate field(s): {', '.join(unknown)}")
    if fields.get("budget_usd") is None:
        raise MandateError("a mandate needs an allocation (budget_usd) — there is no default")
    if not fields.get("chains"):
        raise MandateError("a mandate needs at least one chain")
    return draft(user_id, **fields)


def save_draft(user_id: str, fields: Dict[str, Any], *, origin: str,
               path: Optional[str] = None, now: Optional[float] = None) -> Tuple[str, Mandate]:
    """Validate *fields* and store them as a draft. Returns ``(ref, the mandate it
    would make)``. Raises :class:`MandateError` for a field the rules refuse."""
    m = _validated(user_id, dict(fields))
    clean = {"budget_usd": m.budget_usd, "chains": list(m.chains), "preset": m.preset,
             "ticket_usd": m.ticket_usd, "slots": m.slots,
             "breakers": dict(m.breakers or {})}
    ref = f"d{uuid.uuid4().hex[:10]}"
    from core.sqlite_util import execute_retry, init_schema
    from core.trading_mandate import init_schema as init_mandates
    p = init_mandates(_path(path))  # the whole store, never a file with one table
    init_schema(p, _SCHEMA, mkdir=True)
    execute_retry(p, "INSERT INTO mandate_drafts (ref, user_id, fields, origin, created_at) "
                  "VALUES (?,?,?,?,?)",
                  (ref, str(user_id), json.dumps(clean), str(origin),
                   time.time() if now is None else float(now)))
    return ref, m


def load_draft(ref: str, *, user_id: str, path: Optional[str] = None,
               now: Optional[float] = None) -> Dict[str, Any]:
    """The draft's fields. Raises :class:`MandateError` when there is no such
    draft for this user, it was used, or it expired; :class:`MandateUnreadable`
    when the store cannot be read."""
    p = _path(path)
    try:
        os.stat(p)
    except FileNotFoundError:
        raise MandateError(f"no mandate draft {ref!r} — start again with /trade start") from None
    except OSError as exc:
        raise MandateUnreadable(f"trading store unreachable: {exc}") from exc
    try:
        from core.sqlite_util import execute_retry
        have = execute_retry(p, "SELECT 1 FROM sqlite_master WHERE type='table' AND "
                             "name='mandate_drafts'", (), fetch="one")
        r = execute_retry(p, "SELECT * FROM mandate_drafts WHERE ref=?", (str(ref),),
                          fetch="one") if have else None
    except Exception as exc:
        raise MandateUnreadable(f"trading store unreadable: {exc}") from exc
    if r is None or str(r["user_id"]) != str(user_id):
        raise MandateError(f"no mandate draft {ref!r} — start again with /trade start")
    if r["used_at"] is not None:
        raise MandateError(f"mandate draft {ref} was already used (mandate {r['mandate_id']})")
    ts = time.time() if now is None else float(now)
    if ts - float(r["created_at"]) > DRAFT_TTL_S:
        raise MandateError(f"mandate draft {ref} expired — start again with /trade start")
    return json.loads(r["fields"] or "{}")


def mark_used(ref: str, mandate_id: str, *, path: Optional[str] = None,
              now: Optional[float] = None) -> None:
    from core.sqlite_util import execute_retry
    execute_retry(_path(path), "UPDATE mandate_drafts SET used_at=?, mandate_id=? WHERE ref=?",
                  (time.time() if now is None else float(now), str(mandate_id), str(ref)))


# -- the seat's words -----------------------------------------------------------------

_KEYS = {"budget": "budget_usd", "budget_usd": "budget_usd", "allocation": "budget_usd",
         "chains": "chains", "chain": "chains", "on": "chains", "preset": "preset",
         "style": "preset", "ticket": "ticket_usd", "ticket_usd": "ticket_usd",
         "slots": "slots", "open": "slots"}


def _number(word: str) -> Optional[float]:
    w = str(word or "").strip().lstrip("$").replace(",", "")
    try:
        return float(w)
    except ValueError:
        return None


def parse_start_args(args: Sequence[str]) -> Dict[str, Any]:
    """``/trade start`` words -> draft fields. ``key=value`` words
    (``budget=150 chains=solana,robinhood preset=memecoin ticket=3 slots=8``, plus
    any tunable breaker such as ``daily_loss_pct=8``) or bare words: the first
    number is the allocation, a preset name is the preset, anything else names
    chains. Raises :class:`MandateError`."""
    from core.trading_presets import PRESETS, TUNABLE
    out: Dict[str, Any] = {}
    chains: List[str] = []
    breakers: Dict[str, Any] = {}
    for raw in args or ():
        word = str(raw or "").strip()
        if not word:
            continue
        key, sep, value = word.partition("=")
        if sep:
            k = key.strip().lower()
            if k in _KEYS:
                field = _KEYS[k]
                if field == "chains":
                    chains += [c for c in value.replace("+", ",").split(",") if c.strip()]
                elif field == "preset":
                    out["preset"] = value.strip().lower()
                else:
                    n = _number(value)
                    if n is None:
                        raise MandateError(f"{k} must be a number (got {value!r})")
                    out[field] = int(n) if field == "slots" else n
            elif k in TUNABLE and k not in ("ticket_usd", "slots"):
                n = _number(value)
                if n is None:
                    raise MandateError(f"{k} must be a number (got {value!r})")
                breakers[k] = n
            else:
                raise MandateError(f"unknown field {k!r} — use budget= chains= preset= "
                                   f"ticket= slots=")
            continue
        n = _number(word)
        if n is not None and "budget_usd" not in out:
            out["budget_usd"] = n
        elif word.lower() in PRESETS:
            out["preset"] = word.lower()
        elif n is None and word.lower() not in ("and", "with", "on", "usd"):
            chains += [c for c in word.replace("+", ",").split(",") if c.strip()]
        else:
            raise MandateError(f"I could not read {word!r} — use budget= chains= preset= "
                               f"ticket= slots=")
    if chains:
        out["chains"] = [c.strip().lower() for c in chains]
    if breakers:
        out["breakers"] = breakers
    return out


# -- the echo (086 §3.1) -----------------------------------------------------------------

def _money(v: float) -> str:
    return f"${v:,.2f}".replace(".00", "")


def echo_line(m: Mandate) -> str:
    """The one-line echo of §3.1, every number from the mandate and its preset."""
    from core.trading_mandate import stage_ticket
    p = m.effective_preset()
    chains = " + ".join(c.capitalize() for c in m.chains)
    bits = [f"{m.preset} on {chains}", f"{_money(m.budget_usd)} allocation",
            f"{_money(m.ticket_usd)} tickets", f"up to {m.slots} open"]
    e = p.exit
    if e is not None:
        ex = [f"{e.stop_pct:g}% stop"]
        for gain, sell in e.take_profit:
            ex.append(f"+{gain:g}% sell {sell:g}%")
        if e.trail_pct:
            ex.append(f"trail {e.trail_pct:g}% from the high after +{e.trail_arm_pct or 0:g}%")
        if e.time_stop_h:
            tail = (f" if never +{e.time_stop_min_gain_pct:g}%"
                    if e.time_stop_min_gain_pct is not None else "")
            ex.append(f"{e.time_stop_h:g} h time stop{tail}")
        bits.append(" / ".join(ex))
    else:
        bits.append("no exit barriers (accumulate)")
    b = p.breakers
    if b.daily_loss_pct:
        bits.append(f"daily stop at -{b.daily_loss_pct:g}% of the allocation")
    head = "Got it — " + ", ".join(bits) + "."
    more: List[str] = []
    if b.max_entries_per_day:
        more.append(f"At most {b.max_entries_per_day} entries a day")
    if e is not None:
        loss = float(m.ticket_usd) * abs(float(e.stop_pct)) / 100.0
        more.append(f"one stopped-out ticket costs about {_money(loss)}")
    out = head + (" " + "; ".join(more) + "." if more else "")
    out += (f" It starts at the probe stage ({_money(stage_ticket(m, p))} tickets) until one "
            f"round trip passed on every chain.")
    return out


def card_body(m: Mandate) -> str:
    p = m.effective_preset()
    b = p.breakers
    lines = [echo_line(m), ""]
    stops = []
    if b.drawdown_pct:
        stops.append(f"drawdown -{b.drawdown_pct:g}% of the allocation")
    if b.max_consecutive_losses:
        stops.append(f"{b.max_consecutive_losses} losers in a row -> a {b.cooldown_h or 0:g} h "
                     f"cooldown")
    if b.cost_over_alloc_pct:
        stops.append(f"the LLM cost above {b.cost_over_alloc_pct:g}% of the allocation")
    if stops:
        lines.append("Entries also stop on: " + "; ".join(stops) + ". Exits always run.")
    cadence = f"a hunt every {p.hunt_cadence_min} min"
    if p.exit is not None and p.exit_cadence_min:
        cadence += f", an exit watch every {p.exit_cadence_min} min per open position"
    lines.append(f"Confirm creates the mandate and its jobs: {cadence} (a quiet tick costs "
                 f"$0), and one daily review. Caps stay the wallet's own.")
    return "\n".join(lines)


def intake_card(user_id: str, fields: Dict[str, Any], *, origin: str,
                session_id: Optional[str] = None, path: Optional[str] = None,
                st: Any = None):
    """Validate, store the draft and make the ONE owner card. Returns
    ``(card, mandate it would make)``. Raises :class:`MandateError`."""
    from core.surfaces import cards
    ref, m = save_draft(user_id, fields, origin=origin, path=path)
    card = cards.job_card(user_id, ["/trade", "start", ref],
                          title="Start trading — confirm the mandate", body=card_body(m),
                          session_id=session_id, st=st,
                          origin=cards.ORIGIN_AGENT if origin == "agent"
                          else cards.ORIGIN_SYSTEM)
    return card, m


__all__ = ["DRAFT_TTL_S", "FIELDS", "card_body", "echo_line", "intake_card", "load_draft",
           "mark_used", "parse_start_args", "save_draft"]
