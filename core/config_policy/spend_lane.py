"""The tiered on-chain spend lane (proposal 023 §5.3 D3).

`PAYMENT_APPROVAL_TOOLS` puts every `defi_trade` verb on the durable
`owner_queue` pre-approval lane in EVERY mode. That is the right default —
an on-chain send is irreversible and self-custodial, with no facilitator to
dispute it with — but it has two costs the owner may deliberately choose to
pay:

1. It gates the SIMULATION too. The hook matches on action name alone, so a
   `dry_run=True` swap — which returns from `_run_guarded` *before*
   `sign_and_send` and provably broadcasts nothing — also waits on an owner
   tap. Blocking a quote buys no safety at all, so the dry-run exemption here
   is UNCONDITIONAL.
2. It makes unattended trading impossible. A goal that runs at 03:00 blocks
   on a tap that is not coming, times out after
   ``payment_approval_timeout_sec()`` and is denied.

`DEFI_TIERED_SPEND_LANE` (default OFF) is the explicit owner decision the
023 T4 note reserved: below the per-transaction autonomous ceiling
(``DEFI_AUTONOMOUS_MAX_USD``) a live spend runs act-and-report; above it the
owner queue is unchanged. Nothing else is relaxed — ``tx_guard`` still
simulates every transaction, asserts the observed asset and allowance deltas
against the declared intent, applies PolicyGate and the rolling daily cap,
and independently re-refuses anything over the SAME ceiling with
``lane="owner_queue"``. The declared ``max_spend_usd`` is a sound tiering key
precisely because tx_guard holds the caller to it: a call that under-declares
to buy its way into this lane is refused by the simulation, not executed.

Pure policy — no imports beyond the env helpers, so every tier can read it.
"""
from typing import Any, Dict, Optional

from core.env import bool_env, float_env

#: The on-chain spend verbs this lane can exempt. RUNTIME action names —
#: container tools register as ``{tool_id}_{action}``. Deliberately NOT the
#: venue order verbs (hyperliquid/polymarket) or ``x402_pay``: this lane is
#: scoped to the simulate-and-assert paths — tx_guard for the EVM verbs, and
#: solana_swap's mirrored guard (2026-08-27), which values the simulated
#: outflow and holds it to the declared ``max_spend_usd`` before signing.
DEFI_SPEND_VERBS = frozenset({
    "defi_trade_swap",
    "defi_trade_solana_swap",
    "defi_trade_transfer",
    "defi_trade_approve_token",
    "defi_trade_revoke_approval",
})

#: ``revoke_approval`` sets an allowance to ZERO. It grants nothing and moves
#: no token; its only effect is to retire a standing claim on the wallet. It
#: carries no ``max_spend_usd`` for that reason, so it is tiered on identity.
_RISK_REDUCING_VERBS = frozenset({"defi_trade_revoke_approval"})

#: The x402 auto-pay verb. It is on `PAYMENT_APPROVAL_TOOLS` (H1a) so an
#: above-ceiling payment reaches the owner queue, but a micro-payment must not:
#: blocking every $0.001 fetch on an owner tap makes x402 unusable and times out
#: on any unattended run.
X402_SPEND_VERBS = frozenset({"x402_pay_x402_fetch"})


def tiered_spend_lane_enabled() -> bool:
    """Whether a within-ceiling live spend may skip the owner-queue tap."""
    return bool_env("DEFI_TIERED_SPEND_LANE", False)


def autonomous_ceiling_usd() -> float:
    """The per-transaction ceiling, read from the SAME env var tx_guard reads.

    Kept in sync by construction rather than by remembering: a divergence
    would let this lane wave through a call tx_guard then refuses.
    """
    return float_env("DEFI_AUTONOMOUS_MAX_USD", 25.0)


def x402_autonomous_ceiling_usd() -> float:
    """Per-payment ceiling below which an x402 fetch runs act-and-report.

    Deliberately far smaller than DEFI_AUTONOMOUS_MAX_USD: an x402 payment is
    NOT tx_guard-simulated. Its only bounds are this ceiling, the PolicyGate
    per-tx ceiling, and the rolling daily cap — so the autonomous slice must be
    small enough that a looped drain is bounded by the daily cap long before it
    matters.
    """
    return float_env("X402_AUTONOMOUS_MAX_USD", 1.0)


def _is_simulation(params: Dict[str, Any]) -> bool:
    """True when this call cannot broadcast.

    ``dry_run`` defaults to **True** on every spend param model, so an ABSENT
    key means simulate. `tests/unit/core/test_spend_lane.py` pins those
    defaults, so a model that ever flipped one to False fails the suite rather
    than silently widening this exemption.
    """
    return bool(params.get("dry_run", True))


def spend_exemption(action_name: str,
                    params: Optional[Dict[str, Any]]) -> Optional[str]:
    """Why this call may bypass the owner queue — or None to keep the tap.

    None is the safe answer: the caller treats it as "gate normally", so every
    unrecognised verb, malformed param set and undeclared amount keeps today's
    behaviour.

    Two families, deliberately NOT unified:
      * ``DEFI_SPEND_VERBS`` — tx_guard-simulated; exempt on dry-run
        unconditionally, and on a declared ceiling only under
        ``DEFI_TIERED_SPEND_LANE``.
      * ``X402_SPEND_VERBS`` — NOT simulated; exempt only below the much smaller
        ``X402_AUTONOMOUS_MAX_USD``, with no flag, because the alternative
        default is "every micro-payment blocks".
    """
    params = params or {}
    if action_name in X402_SPEND_VERBS:
        return _x402_exemption(params)
    if action_name not in DEFI_SPEND_VERBS:
        return None

    if _is_simulation(params):
        return ("dry run — the guard is consulted but nothing is broadcast")

    if not tiered_spend_lane_enabled():
        return None

    if action_name in _RISK_REDUCING_VERBS:
        return "revoke — sets the allowance to zero, so it can only reduce risk"

    declared = params.get("max_spend_usd")
    if not isinstance(declared, (int, float)) or isinstance(declared, bool):
        return None
    if declared <= 0:
        return None

    ceiling = autonomous_ceiling_usd()
    if declared <= ceiling:
        return (f"declared ceiling ${declared:.4f} is within the ${ceiling:.2f} "
                f"autonomous limit")
    return None


#: Back-compat alias — `tools/controller/service.py` and existing tests import
#: this name. Kept so the rename is not a breaking change for any importer.
defi_spend_exemption = spend_exemption


def _x402_exemption(params: Dict[str, Any]) -> Optional[str]:
    """x402 auto-pay: exempt only strictly inside the small autonomous ceiling.

    Keyed on ``max_amount_usd`` — the agent's own declared authorization — which
    is a sound tiering key here ONLY because `tools/x402/service.py` authorizes
    the PolicyGate at exactly that worst case (`check_amount = max_amount_usd`),
    so a call cannot under-declare to buy into this lane and then pay more.
    """
    declared = params.get("max_amount_usd")
    if not isinstance(declared, (int, float)) or isinstance(declared, bool):
        return None
    if declared <= 0:
        return None
    ceiling = x402_autonomous_ceiling_usd()
    if declared <= ceiling:
        return (f"x402 micro-payment: declared ceiling ${declared:.4f} is within "
                f"the ${ceiling:.2f} autonomous limit")
    return None
