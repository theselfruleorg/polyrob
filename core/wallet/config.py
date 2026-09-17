"""Agent-wallet configuration (env-driven, default-safe)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Dict, Mapping, Optional

TESTNET_FACILITATOR_URL = "https://x402.org/facilitator"

from core.env import bool_from as _bool_from, parse_opt_float as _parse_opt_float, float_from as _float_from


def _b(env: Mapping[str, str], key: str, default: bool) -> bool:
    # Repo-SSOT falsey-set semantics (core.env) — the old private {1,true,yes,on}
    # opt-in set disagreed with every other flag parser on values like "enabled".
    return _bool_from(env, key, default)


@dataclass(frozen=True)
class WalletConfig:
    enabled: bool
    backend: str
    # L1 (2026-07-15): repr=False so the auto-generated __repr__ never embeds the
    # raw seed (still a required field — no default; kept before the defaulted
    # fields below). One logger.debug(f"...{cfg}") away from a log leak otherwise.
    master_seed: Optional[str] = field(repr=False)
    network: str
    max_per_tx_usd: float
    x402_client_enabled: bool
    x402_facilitator_url: str
    daily_cap_usd: Optional[float] = None
    per_venue_daily_cap_usd: Dict[str, float] = field(default_factory=dict)
    # The venue key that same-chain spend paths (x402, generic payments) SIGN with.
    # Default "treasury" so the address the owner funds (== AgentWallet.address) is the
    # address actually spent from. "Venue" elsewhere (policy caps) stays an accounting
    # label. Hyperliquid keeps its own delegated key regardless of this.
    operational_venue: str = "treasury"
    # 2026-09-18: re-resolves ``(max_per_tx_usd, daily_cap_usd)`` on demand so
    # PolicyGate honours an owner pref the moment it is approved (see
    # ``live_caps_resolver``). ``load_wallet_config`` sets it; a hand-built
    # config (tests, fakes) keeps None = the two frozen fields above.
    cap_resolver: Optional[Callable[[], tuple]] = field(default=None, repr=False,
                                                        compare=False)


#: Rolling-24h aggregate spend bound. FINITE by default (H3): the per-tx ceiling
#: is a catastrophe stop, not a budget, and it cannot stop a within-ceiling loop.
DEFAULT_DAILY_CAP_USD = 100.0
#: Per-transaction catastrophe stop. Was 1000.0; lowered because $1000 in one
#: transaction is not "catastrophe-only" on this treasury.
DEFAULT_MAX_PER_TX_USD = 250.0

#: Explicit "no aggregate bound" sentinels. Absence used to mean this; now the
#: operator has to write it, so the decision is visible in the env file.
#: NOTE "0" is deliberately NOT a sentinel (controller ruling R5): it reads as
#: "no spend permitted" at least as naturally as "no cap", and a money flag must
#: never guess. WALLET_DAILY_CAP_USD=0 is a literal $0 cap that refuses all spend.
_CAP_DISABLED = frozenset({"none", "off", "unlimited", "disabled"})


def _cap_float(env: Mapping[str, str], key: str, default: float) -> Optional[float]:
    """Money cap parse: unset/blank -> *default*; an explicit disable sentinel ->
    None (no cap); anything else must be a finite, non-negative number or raise
    naming *key* and the offending value.

    H3-sub (audit 2026-08-22): the old `_opt_float` returned None on garbage, so a
    typo'd `WALLET_DAILY_CAP_USD=1O0` silently meant NO CAP while the owner
    believed one was active. The per-tx ceiling already raised loudly; this is
    parity for the aggregate cap. Minor 4 (fix round 1, 2026-08-22 review): a
    negative cap is never meaningful (there is no such thing as "spend less
    than nothing") — refuse it the same way, rather than silently accepting a
    ceiling that can never be reached.
    """
    raw = env.get(key)
    if raw is None or not str(raw).strip():
        return default
    if str(raw).strip().lower() in _CAP_DISABLED:
        return None
    val = _parse_opt_float(raw)
    if val is None:
        raise ValueError(f"{key} is not a finite number: {raw!r}")
    if val < 0:
        raise ValueError(f"{key} must not be negative: {raw!r}")
    return val


def _req_float(env: Mapping[str, str], key: str, default: float) -> float:
    """Loud float parse for money ceilings: unset/blank -> *default*; set but
    non-numeric, non-finite, OR negative -> ValueError naming the key. The CLI
    wallet view's misconfig branch (M12) RELIES on this raise to tell the
    owner which env key is broken — a silent fallback here would turn a
    typo'd cap into the default without anyone noticing. Minor 4 (fix round
    1, 2026-08-22 review): a negative ceiling is never meaningful."""
    raw = env.get(key)
    if raw is None or not str(raw).strip():
        return default
    val = _parse_opt_float(raw)
    if val is None:
        raise ValueError(f"{key} is not a finite number: {raw!r}")
    if val < 0:
        raise ValueError(f"{key} must not be negative: {raw!r}")
    return val


def _venue_cap_float(env: Mapping[str, str], key: str) -> Optional[float]:
    """Per-venue cap parse (``WALLET_VENUE_DAILY_CAP_<VENUE>_USD``): unset/
    blank -> None (no venue-specific cap — there is no built-in per-venue
    default, absence already means "global daily cap only"); an explicit
    disable sentinel -> also None, same effect, so an operator can say so
    explicitly for clarity; anything else must be a finite, non-negative
    number or raise naming *key* and the offending value.

    Minor 2 (fix round 1, 2026-08-22 review): this was the LAST silent-cap-
    drop in the file — the old `_opt_float` made
    `WALLET_VENUE_DAILY_CAP_HYPERLIQUID_USD=1O0` silently drop that venue's
    cap entirely (parsed to None, indistinguishable from "no cap set"), one
    function away from the H3 fix applied to the daily/per-tx caps above.
    """
    raw = env.get(key)
    if raw is None or not str(raw).strip():
        return None
    if str(raw).strip().lower() in _CAP_DISABLED:
        return None
    val = _parse_opt_float(raw)
    if val is None:
        raise ValueError(f"{key} is not a finite number: {raw!r}")
    if val < 0:
        raise ValueError(f"{key} must not be negative: {raw!r}")
    return val


def _load_per_venue_caps(env: Mapping[str, str]) -> Dict[str, float]:
    """Parse WALLET_VENUE_DAILY_CAP_<VENUE>_USD env vars into {venue: cap}."""
    prefix, suffix = "WALLET_VENUE_DAILY_CAP_", "_USD"
    caps: Dict[str, float] = {}
    for key in env:
        if key.startswith(prefix) and key.endswith(suffix):
            venue = key[len(prefix):-len(suffix)].lower()
            val = _venue_cap_float(env, key)
            if venue and val is not None:
                caps[venue] = val
    return caps


def effective_daily_cap_usd(user_id: Optional[str], home_dir,
                            env: Optional[Mapping[str, str]] = None) -> Optional[float]:
    """Owner's rolling-24h wallet spend cap: pref (min-merged, spec
    ``budget.wallet_daily_usd``) over ``WALLET_DAILY_CAP_USD``.

    H3 (2026-08-22): ``WALLET_DAILY_CAP_USD`` now has a FINITE default
    (``DEFAULT_DAILY_CAP_USD``), so the env leg passed to the min-merge is the
    *resolved* value (default when unset, an explicit sentinel's ``None`` when
    the operator genuinely disabled it, or the parsed number) — never a bare
    ``None`` standing in for "unset". Passing the resolved default here (not
    ``None``) is what stops a pref ALONE from raising the effective cap above
    the default: with ``env_value=100.0`` and a wider pref of ``500.0``, the
    "min" merge below still resolves 100.0, not 500.0. A pref can still SET a
    cap where the operator explicitly disabled one (``env_value=None`` when the
    sentinel is used) — that is still only ever a tightening, from unlimited.
    No pref file present => byte-identical to
    ``load_wallet_config(env).daily_cap_usd`` (owner-UX P1 T4). Wired into
    ``load_wallet_config`` -> ``PolicyGate`` (G-13)."""
    from core import prefs
    env_value = _cap_float(os.environ if env is None else env,
                           "WALLET_DAILY_CAP_USD", DEFAULT_DAILY_CAP_USD)
    # `default=None` (not DEFAULT_DAILY_CAP_USD) is deliberate: this fallback
    # only fires when env_value IS None, which — now that unset resolves
    # through the default above — happens ONLY when the operator wrote an
    # explicit disable sentinel. Respect that: with no pref file either, the
    # cap stays genuinely disabled, not silently reinstated at the default.
    return prefs.resolve("budget.wallet_daily_usd", user_id, home_dir,
                         env_value=env_value, default=None)


def effective_max_per_tx_usd(user_id: Optional[str], home_dir,
                             env: Optional[Mapping[str, str]] = None) -> float:
    """Owner's per-transaction wallet ceiling: an owner-approved pref
    (``budget.wallet_per_tx_usd``, guarded — only an owner tap writes it) over
    the ``AGENT_WALLET_MAX_PER_TX_USD`` default, **clamped to the daily cap**.

    Until 2026-09-18 this was a pure min-merge, so a pref could only LOWER the
    env value. The owner approved a raise from chat twice (2026-09-09,
    2026-09-17) and both times the tap changed nothing; the agent then asked
    the owner to edit the env file and restart the service. The owner's ruling:
    an owner tap raises it, bounded by the daily cap. The daily cap
    (:func:`effective_daily_cap_usd`) stays min-merged and env-only, so it is
    the operator's hard envelope: the largest single transaction is at most
    what a day may lose, and no raise made from chat moves the maximum daily
    loss. With the daily cap explicitly disabled (``WALLET_DAILY_CAP_USD=none``)
    there is no envelope to clamp to — the operator opted out of it.

    The env leg is ALWAYS a concrete value (the $250 catastrophic-loss default
    when unset — not a "no cap" sentinel; H3 2026-08-22, was $1000). No pref
    file present => byte-identical to ``load_wallet_config(env).max_per_tx_usd``
    (owner-UX G-13)."""
    from core import prefs
    env_map = os.environ if env is None else env
    env_value = _req_float(env_map, "AGENT_WALLET_MAX_PER_TX_USD",
                           DEFAULT_MAX_PER_TX_USD)
    value = prefs.resolve("budget.wallet_per_tx_usd", user_id, home_dir,
                          env_value=env_value, default=env_value)
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = env_value
    if value > env_value:
        # A RAISE above the operator default is the only case the daily
        # envelope must bound; a pref at or below it was always allowed.
        daily = effective_daily_cap_usd(user_id, home_dir, env=env_map)
        if daily is not None and daily > 0 and value > daily:
            value = daily
    return value


def _fail_open_owner_user_id() -> Optional[str]:
    """Representative tenant for the process-level wallet singleton's pref
    lookup (the agent wallet is single/operator-owned — see
    ``core/wallet/factory.py``). Fail-open to ``None`` => no pref match =>
    legacy env-only value, mirroring ``agents.task.goals.dispatcher._tick_owner_user_id``.

    ⚠️ Reads the ONE owner-tenant resolver (``resolve_owner_user_id``), not the
    owner PRINCIPAL. This is money-adjacent: the console writes
    ``budget.wallet_*`` under the tenant it reads (now ``local`` when unbound),
    and a ceiling that stops applying falls back to the env value — which can be
    WIDER than the pref the owner set.
    """
    try:
        from core.instance import resolve_owner_user_id
        return resolve_owner_user_id()
    except Exception:
        return None


def _fail_open_home_dir():
    """Pref-storage home for the process-level wallet singleton. Fail-open to
    ``None`` => legacy env-only value (any downstream error is ALSO caught by
    the caller's own try/except, so this can never crash config loading)."""
    try:
        # Same home the pref writers use (the data home), never $HOME/.polyrob —
        # a reader on a different tree cannot see the owner's own cap.
        from core.runtime_paths import prefs_home_dir
        return prefs_home_dir()
    except Exception:
        return None


def live_caps_resolver(env: Optional[Mapping[str, str]] = None, *,
                       user_id: Optional[str] = None,
                       home_dir: Optional[object] = None):
    """A zero-arg callable returning ``(max_per_tx_usd, daily_cap_usd)`` as
    they are RIGHT NOW — the same two resolutions ``load_wallet_config`` makes,
    re-run on demand.

    ``PolicyGate`` froze both caps at construction (process start), so the
    ``budget.wallet_*`` prefs — documented ``applies: live`` and shown so by
    ``preferences explain`` — did not apply until the next restart, in EITHER
    direction: an owner tightening the ceiling from chat was as ineffective as
    one raising it (found 2026-09-18). The gate now asks this resolver on every
    check. The pref store is mtime-cached, so the cost is a stat.

    Returns ``None`` on either leg it cannot resolve; the gate keeps its
    constructed value for that leg (fail-open to the value it already had —
    never to a wider one it invented).
    """
    env_map = os.environ if env is None else env

    def _resolve():
        resolved_user = user_id if user_id is not None else _fail_open_owner_user_id()
        resolved_home = home_dir if home_dir is not None else _fail_open_home_dir()
        per_tx = daily = None
        try:
            per_tx = effective_max_per_tx_usd(resolved_user, resolved_home, env=env_map)
        except Exception:
            pass
        try:
            daily = effective_daily_cap_usd(resolved_user, resolved_home, env=env_map)
        except Exception:
            daily = _UNRESOLVED
        return per_tx, daily
    return _resolve


#: Sentinel: the daily leg could not be resolved (distinct from ``None`` =
#: "resolved: the operator disabled the cap").
_UNRESOLVED = object()


def load_wallet_config(env: Optional[Mapping[str, str]] = None, *,
                       user_id: Optional[str] = None,
                       home_dir: Optional[object] = None) -> WalletConfig:
    """Build the wallet config PolicyGate is built from.

    ``daily_cap_usd``/``max_per_tx_usd`` are resolved through
    :func:`effective_daily_cap_usd`/:func:`effective_max_per_tx_usd` (G-13,
    owner-UX): a per-tenant preference can only TIGHTEN these two
    env-authoritative caps, never raise or disable them. ``user_id``/``home_dir``
    default to a fail-open owner/home resolution (this is a process-level,
    single-owner singleton — see ``core/wallet/factory.py``); ANY failure in
    that resolution, or in the prefs module itself, leaves the plain env value
    completely unchanged (fail-open — prefs are advisory, never a crash risk
    for money config).
    """
    env = os.environ if env is None else env
    network = env.get("AGENT_WALLET_NETWORK", "testnet").strip().lower()
    # Safety default: a catastrophic per-tx ceiling, NOT a budget. Was
    # $1,000,000, then $1000 (H3, 2026-08-22: $1000 in one transaction is not
    # "catastrophe-only" on this treasury); raise it explicitly if needed.
    max_per_tx_usd = _req_float(env, "AGENT_WALLET_MAX_PER_TX_USD", DEFAULT_MAX_PER_TX_USD)
    # H3 (2026-08-22): rolling 24h spend cap now has a FINITE default — the
    # per-tx ceiling is a catastrophe stop, not a budget, and cannot alone stop
    # a within-ceiling loop draining the treasury one ticket at a time (x402's
    # idempotency key is URL-keyed, so a loop mints a fresh key every
    # iteration). WALLET_DAILY_CAP_USD=none/off/unlimited/disabled restores the
    # old unbounded behaviour explicitly; a set-but-unparseable value raises.
    daily_cap_usd = _cap_float(env, "WALLET_DAILY_CAP_USD", DEFAULT_DAILY_CAP_USD)
    resolved_user = user_id if user_id is not None else _fail_open_owner_user_id()
    resolved_home = home_dir if home_dir is not None else _fail_open_home_dir()
    try:
        daily_cap_usd = effective_daily_cap_usd(resolved_user, resolved_home, env=env)
    except Exception:
        pass  # fail-open: prefs unavailable/raising -> env value unchanged
    try:
        max_per_tx_usd = effective_max_per_tx_usd(resolved_user, resolved_home, env=env)
    except Exception:
        pass  # fail-open: prefs unavailable/raising -> env value unchanged
    return WalletConfig(
        enabled=_b(env, "AGENT_WALLET_ENABLED", False),
        backend=env.get("AGENT_WALLET_BACKEND", "local_eoa").strip().lower(),
        master_seed=env.get("AGENT_WALLET_MASTER_SEED"),
        network=network if network in ("testnet", "mainnet") else "testnet",
        max_per_tx_usd=max_per_tx_usd,
        x402_client_enabled=_b(env, "X402_CLIENT_ENABLED", False),
        x402_facilitator_url=env.get("X402_CLIENT_FACILITATOR_URL", TESTNET_FACILITATOR_URL),
        daily_cap_usd=daily_cap_usd,
        per_venue_daily_cap_usd=_load_per_venue_caps(env),
        operational_venue=(env.get("AGENT_WALLET_OPERATIONAL_VENUE", "treasury").strip().lower()
                           or "treasury"),
        cap_resolver=live_caps_resolver(env, user_id=user_id, home_dir=home_dir),
    )
