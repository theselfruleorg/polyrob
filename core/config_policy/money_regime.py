"""The money regime — ONE derived answer to "may unattended work move funds here?".

Sibling of ``autonomy_mode.py`` / ``builder_mode.py``. It adds NO flag: it reads
the flags that already exist and names the regime they add up to, so the gates
on the goal / cron / rig path key off one predicate instead of each re-deriving
a subset of it (the 2026-09-23 security pass added four gates that did exactly
that, and an owner who had armed every flag still saw ``money_rail`` refused).

    supervised — ``AUTONOMY_MODE`` is not effectively ``autonomous``. Work the
                 AGENT writes (goals, cron jobs, rigs) never carries a money
                 tool. The owner may still grant one from an owner seat.
    autonomous — the single-owner act-and-report instance
                 (:func:`full_autonomy_enabled`), money NOT armed: the agent
                 schedules its own recurring work, but its self-authored work
                 cannot trade unattended.
    armed      — autonomous + every key the unattended trade path checks:

                 * ``DEFI_AGENT_AUTONOMY``        — the grant (``defi_trade`` in
                   the autonomous toolset, so a self-authored ``money_rail``
                   rig passes the ceiling);
                 * ``DEFI_TRADE_ENABLED``         — the tool registers at all;
                 * ``DEFI_AUTONOMOUS_TURN_TRADING`` — ``tx_guard`` step 2 lets a
                   goal/cron-dispatched MAIN-agent turn sign;
                 * ``DEFI_TIERED_SPEND_LANE``     — a within-ceiling spend runs
                   act-and-report instead of waiting on a tap at 03:00;
                 * a daily cap                   — ``tx_guard`` step 9 refuses
                   the autonomous lane without an aggregate damage bound.

⚠️ What bounds the armed regime is the CAPS (per-tx, daily,
``DEFI_AUTONOMOUS_MAX_USD``), the per-verb simulation and the owner pause —
never a toolset refusal. What it NEVER relaxes: a correspondent-tainted,
forged (self-wake / delegation-result), room, leaf or sub-agent turn is refused
in every regime. Injected intent is not owner intent.

``missing`` names the keys an autonomous instance still lacks, so a partial
arm (the common failure: the grant without the turn bar, or the turn bar
without the lane) is SAID on every status seat instead of surfacing as a
refusal inside a goal run.
"""
from core.config_policy.autonomy_mode import autonomous_work_enabled  # noqa: F401 — moved (067 P1b)
from core.money.regime import (  # noqa: F401 — the shape lives in the money kernel
    REGIME_ARMED,
    REGIME_AUTONOMOUS,
    REGIME_SUPERVISED,
    MoneyRegime,
    register_arming_keys,
    resolve_regime,
)


def _daily_cap_set() -> bool:
    """True unless the operator disabled the rolling daily cap. Fail-closed:
    an unresolvable cap counts as absent (tx_guard would refuse too)."""
    try:
        from core.wallet.config import effective_daily_cap_usd
        return effective_daily_cap_usd(None, None) is not None
    except Exception:
        return False


def _env_key(name: str):
    def _check() -> bool:
        from core.env import bool_env
        return bool_env(name, False)
    return _check


# 067 P1b: the keys the unattended DeFi trade path checks, contributed to the
# kernel's regime (order = the order ``missing`` names them).
register_arming_keys({
    "DEFI_AGENT_AUTONOMY": _env_key("DEFI_AGENT_AUTONOMY"),
    "DEFI_TRADE_ENABLED": _env_key("DEFI_TRADE_ENABLED"),
    "DEFI_AUTONOMOUS_TURN_TRADING": _env_key("DEFI_AUTONOMOUS_TURN_TRADING"),
    "DEFI_TIERED_SPEND_LANE": _env_key("DEFI_TIERED_SPEND_LANE"),
    "WALLET_DAILY_CAP_USD": lambda: _daily_cap_set(),
})


def money_regime() -> MoneyRegime:
    """Resolve the regime from the live env. Pure read; access-time."""
    from core.config_policy.autonomy_mode import full_autonomy_enabled
    return resolve_regime(full_autonomy_enabled())


def autonomous_money_armed() -> bool:
    """Every key the unattended trade path checks is set."""
    return money_regime().name == REGIME_ARMED


def money_regime_display() -> str:
    """One line for the status seats (posture card)."""
    regime = money_regime()
    if regime.name == REGIME_SUPERVISED:
        return "supervised — agent-written goals/cron cannot hold money tools"
    if regime.name == REGIME_ARMED:
        try:
            from core.config_policy.spend_lane import autonomous_ceiling_usd
            ceiling = f" (act-and-report ≤ ${autonomous_ceiling_usd():g}/tx, above → owner queue)"
        except Exception:
            ceiling = ""
        return "armed — goals and cron may trade unattended within caps" + ceiling
    return "autonomous, money NOT armed — missing " + ", ".join(regime.missing)
