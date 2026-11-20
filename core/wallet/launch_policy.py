"""A launch with a quote token — the owner pin and the launch ceilings (proposal 122 W3).

A launch (``defi_trade.launch_token``) deploys the fixed-supply template token and seeds a
Uniswap v4 pool token/quote on the 117 seed path. Two transactions carry a launch mark
(``TxIntent.launch``): the deploy ``("deploy", quote)`` and the seed ``("seed", currency0,
currency1)``. The money guard (``tx_guard_steps.value.step_7a_launch_ceiling``) holds both
to the rules here; the verb reads the same rules first, so a refusal names the rule before
anything is sent. There is no second authorizer: these are refusals, never an allow.

Three owner preferences (``core/prefs.py``; an owner pin, not an env flag):

* ``launch.pair`` — ``<chain>:<token>``. When set, every launch is on that chain and pairs to
  that token; any other pair refuses by name. The native coin is the zero address (v4's own
  currency id for it).
* ``launch.per_day`` — the launches in 24 hours (default :data:`DEFAULT_PER_DAY`). Every
  ``deploy_token`` in the wallet's ledger counts, a launch or a plain deploy: over-count,
  never under-count.
* ``launch.max_pool_usd`` — the value of one seed at the guard's price. Unset = the
  per-transaction limit is the only bound.

⚠️ Pure: no import from a module of the large import cycle (``core.prefs`` is one). The
callers read the preferences and pass a ``resolve(key)`` function in.
"""
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Tuple

PAIR_KEY = "launch.pair"
PER_DAY_KEY = "launch.per_day"
POOL_KEY = "launch.max_pool_usd"
#: The action name of the launch verb (the mandate scope names it).
ACTION = "defi_trade_launch_token"
DEFAULT_PER_DAY = 1
NATIVE = "0x" + "0" * 40
#: The ledger action every launch writes once (its deploy).
DEPLOY_ACTION = "deploy_token"
_PAIR_RE = re.compile(r"^([a-z0-9_-]+)[:\s]+(0x[0-9a-fA-F]{40})$")


@dataclass(frozen=True)
class Settings:
    pair: Optional[Tuple[str, str]] = None
    per_day: int = DEFAULT_PER_DAY
    max_pool_usd: Optional[float] = None
    #: Not empty when a preference could not be read: every launch then refuses.
    error: str = ""


def parse_pair(text: Any) -> Optional[Tuple[str, str]]:
    """``"<chain>:<token>"`` → ``(chain, token)`` (lower case), ``""``/None → None.
    Raises ``ValueError`` for any other shape."""
    s = str(text or "").strip()
    if not s:
        return None
    m = _PAIR_RE.fullmatch(s.lower())
    if not m:
        raise ValueError(f"{PAIR_KEY} is <chain>:<token address> (the native coin is {NATIVE}); "
                         f"got {s[:80]!r}")
    return m.group(1), m.group(2)


def read_settings(resolve: Callable[[str], Any]) -> Settings:
    """The three preferences through *resolve*. Never raises: a value that cannot be read
    sets ``error`` (a launch refuses; it never runs without its pin). The keys are literal
    here: ``tests/unit/core/test_prefs_no_dead_keys_ratchet.py`` finds the enforcement read."""
    try:
        pair = parse_pair(resolve("launch.pair"))
        per_day = resolve("launch.per_day")
        per_day = DEFAULT_PER_DAY if per_day is None else int(per_day)
        seed = resolve("launch.max_pool_usd")
        seed = None if seed in (None, "") else float(seed)
        if per_day < 0 or (seed is not None and not seed >= 0):
            raise ValueError("a launch limit is below zero")
        return Settings(pair=pair, per_day=per_day, max_pool_usd=seed)
    except Exception as exc:  # noqa: BLE001 — unreadable is not "no pin"
        return Settings(error=f"the launch preferences could not be read ({exc})")


def read_owner_settings(scope_fn: Callable[[], Any], load_fn: Callable[..., Any],
                        resolve_fn: Callable[..., Any]) -> Settings:
    """The owner's ``launch.*`` preferences — the ONE read for the verb and guard step 7a. The
    callers pass ``core.prefs``' ``owner_pref_scope``, ``load_preferences`` and ``resolve`` (this
    module imports no preference code: it stays out of the largest import cycle). Strict (the
    2026-11-20 review): an owner scope that cannot be resolved, or a preferences file that
    cannot be read, is ``Settings(error=…)`` — every launch refuses — never the defaults."""
    try:
        user_id, home = scope_fn()
        if not user_id or not home:
            raise ValueError("the owner or the preferences home cannot be resolved")
        load_fn(home, user_id, strict=True)   # raises when the file cannot be read
        return read_settings({k: resolve_fn(k, user_id, home)
                              for k in (PAIR_KEY, PER_DAY_KEY, POOL_KEY)}.get)
    except Exception as exc:  # noqa: BLE001 — unreadable is not "no pin"
        return Settings(error=f"the launch preferences could not be read ({exc})")


def pair_refusal(s: Settings, chain: str, currencies: Iterable[str]) -> Optional[str]:
    """Why this pair breaks the ``launch.pair`` pin, or None. *currencies* are the quote
    (a deploy) or both pool currencies (a seed)."""
    if s.error:
        return f"refused: {s.error}"
    if s.pair is None:
        return None
    want_chain, want_token = s.pair
    have = {str(c or "").lower() for c in currencies}
    if str(chain or "").lower() != want_chain or want_token not in have:
        return (f"refused: the owner pinned launch.pair = {want_chain}:{want_token}; this launch "
                f"pairs to {', '.join(sorted(have)) or '?'} on {chain}. A launch pairs only to "
                f"the pinned token on the pinned network")
    return None


def launches_today(audit_log: Iterable[dict], *, now: Optional[float] = None,
                   voided: Iterable[str] = ()) -> int:
    """The ``deploy_token`` rows of the last 24 hours (a reverted one excluded)."""
    now = time.time() if now is None else now
    gone = set(voided or ())
    return sum(1 for e in audit_log or ()
               if e.get("action") == DEPLOY_ACTION and float(e.get("ts") or 0) >= now - 86400
               and not (gone and e.get("result_ref") in gone))


def count_refusal(s: Settings, done_today: int) -> Optional[str]:
    if s.error:
        return f"refused: {s.error}"
    if done_today >= s.per_day:
        return (f"refused: launch.per_day is {s.per_day} and {done_today} token deploy(s) ran in "
                f"the last 24 hours (every deploy_token counts)")
    return None


def seed_refusal(s: Settings, usd: Optional[float]) -> Optional[str]:
    """Why a seed of *usd* breaks ``launch.max_pool_usd``, or None. An unknown value refuses
    when a limit is set."""
    if s.error:
        return f"refused: {s.error}"
    if s.max_pool_usd is None:
        return None
    if usd is None:
        return "refused: launch.max_pool_usd is set and the seed value is unknown"
    if float(usd) > s.max_pool_usd + 1e-9:
        return (f"refused: the seed is worth ${float(usd):,.2f}, above launch.max_pool_usd "
                f"${s.max_pool_usd:,.2f}")
    return None


def gate_launches_today(gate, now: Optional[float] = None) -> int:
    """:func:`launches_today` over a ``PolicyGate`` (its synced durable ledger). Raises
    ``RuntimeError`` when the ledger is unreadable (unknown is not zero)."""
    sync = getattr(gate, "_sync_shared_ledger", None)
    if sync is not None and not sync():
        raise RuntimeError("the spend ledger is unreadable, so today's launches cannot be counted")
    voided_fn = getattr(gate, "_voided", None)
    voided = voided_fn() if callable(voided_fn) else ()
    return launches_today(getattr(gate, "audit_log", ()) or (), now=now, voided=voided)


def guard_refusal(intent, amount_usd: Optional[float], gate, s: Settings,
                  now: Optional[float] = None) -> Optional[str]:
    """The guard's launch ceiling for a marked intent, or None."""
    mark = tuple(getattr(intent, "launch", ()) or ())
    if not mark:
        return None
    kind, currencies = mark[0], mark[1:]
    if kind not in ("deploy", "seed") or not currencies:
        return f"refused: an unknown launch mark {mark!r}"
    why = pair_refusal(s, intent.chain, currencies)
    if why:
        return why
    if kind == "deploy":
        try:
            done = gate_launches_today(gate, now)
        except Exception as exc:  # noqa: BLE001
            return f"refused: {exc}"
        return count_refusal(s, done)
    return seed_refusal(s, amount_usd)


def facts_lines(s: Settings, *, done_today: Optional[int], lp_caps: Optional[tuple]) -> list:
    """The lines a launch card and ``/lp launch`` show."""
    eth = lambda w: f"{w / 1e18:.6g} ETH"
    pin = (f"{s.pair[0]}:{s.pair[1]}" if s.pair else "not set (any pair)")
    out = [f"launch.pair: {pin}",
           f"launches: {'?' if done_today is None else done_today} of {s.per_day} in 24 h",
           "launch.max_pool_usd: " + (f"${s.max_pool_usd:,.2f}" if s.max_pool_usd is not None
                                      else "not set (the per-transaction limit binds)")]
    if s.error:
        out.insert(0, f"⚠️ {s.error} — every launch refuses")
    if lp_caps is not None:
        program, daily, floor = lp_caps
        out.append(("LP_ETH_CAP: " + (eth(program) if program > 0 else
                                      "0 — no native coin goes into a v4 pool")
                    + f"; LP_ETH_DAILY_CAP: {eth(daily)}; LP_ETH_FLOOR: {eth(floor)}"))
    return out


__all__ = ["ACTION", "DEFAULT_PER_DAY", "NATIVE", "PAIR_KEY", "PER_DAY_KEY", "POOL_KEY",
           "Settings", "count_refusal", "facts_lines", "gate_launches_today", "guard_refusal",
           "launches_today", "pair_refusal", "parse_pair", "read_settings",
           "seed_refusal"]
