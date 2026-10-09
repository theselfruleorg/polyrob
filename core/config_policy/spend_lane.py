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

Pure policy — no imports beyond the env helpers and the tier-0 verb-policy
table, so every tier can read it.
"""
import logging
from typing import Any, Dict, Optional

from core.env import bool_env, float_env
from core.lazy_views import lazy_module_getattr, view
from core.verb_policy import ids_where

logger = logging.getLogger(__name__)

# The five verb sets below are DERIVED from the per-action policy table
# (core/verb_policy.py; rows and their per-verb rationale in
# core/verb_policy_rows.py, 067 P1). Classify a verb there, by its ``lane``,
# ``risk_reducing`` and ``simulatable`` fields.
#
# 067 P4 prerequisite: each set is LAZY (``core/lazy_views.py``) — built on first
# read, after the pack loader's phase 1 — through the module ``__getattr__`` at the
# end of this file. Functions here read them with ``view(__name__, NAME)``.

#: ``lane="defi"``: the on-chain spend verbs this lane can exempt. RUNTIME action
#: names — container tools register as ``{tool_id}_{action}``. Deliberately NOT
#: the venue order verbs (hyperliquid/polymarket) or ``x402_pay``: this lane is
#: scoped to the simulate-and-assert paths — tx_guard for the EVM verbs, and
#: solana_swap's mirrored guard (2026-08-27), which values the simulated
#: outflow and holds it to the declared ``max_spend_usd`` before signing.
def _defi_spend_verbs():
    return ids_where(lane="defi")

#: ``lane="owner_always"``: money-spend verbs that are NEVER exemptible by this
#: lane. A defi verb must be on one of the two lanes — the bidirectional
#: contract test (`tests/unit/core/test_money_verb_registration.py`) fails on
#: one that is on neither, so a new verb cannot be forgotten into the wrong
#: lane. Putting a verb on the defi lane instead would let one env flag
#: (`DEFI_TIERED_SPEND_LANE=true`) exempt it below a ceiling.
def _always_owner_approved_verbs():
    return ids_where(lane="owner_always")

#: ``risk_reducing``: verbs that can only retire exposure (a revoke sets an
#: allowance to ZERO; it grants nothing, moves no token and carries no
#: ``max_spend_usd``, so it is tiered on identity).
def _risk_reducing_verbs():
    return ids_where(risk_reducing=True)

#: ``lane="x402"``: the x402 auto-pay verb. It is on `PAYMENT_APPROVAL_TOOLS`
#: (H1a) so an above-ceiling payment reaches the owner queue, but a
#: micro-payment must not: blocking every $0.001 fetch on an owner tap makes
#: x402 unusable and times out on any unattended run.
def _x402_spend_verbs():
    return ids_where(lane="x402")


def tiered_spend_lane_enabled() -> bool:
    """Whether a within-ceiling live spend may skip the owner-queue tap."""
    return bool_env("DEFI_TIERED_SPEND_LANE", False)


#: ``DEFI_AUTONOMOUS_MAX_USD`` default — the ONE copy (tx_guard reads it too).
DEFAULT_DEFI_AUTONOMOUS_MAX_USD = 25.0
#: ``X402_AUTONOMOUS_MAX_USD`` default.
DEFAULT_X402_AUTONOMOUS_MAX_USD = 1.0


def autonomous_ceiling_usd() -> float:
    """The per-transaction autonomous ceiling — the SAME number tx_guard step 9
    reads, resolved the SAME way (owner pref ``budget.defi_autonomous_usd`` over
    ``DEFI_AUTONOMOUS_MAX_USD``, clamped to the per-tx backstop).

    Until 2026-09-18 this read the RAW env var while tx_guard read the pref, so
    the two halves of one lane disagreed: prod's owner approved a $300
    autonomous ceiling from chat, tx_guard honoured it, and this hook still
    demanded a tap for every live swap over the env's $5 — three taps in one
    afternoon for trades the owner had already said may run unattended, and an
    unattended cron buyback that could never run at all. Delegating to
    tx_guard's own resolver keeps the two in sync by construction; a failed
    resolve fails CLOSED to 0.0 (every live spend asks): the raw env value
    would skip the per-tx clamp and a lower owner pref.
    """
    try:
        # Lazy: core.wallet.tx_guard imports core.config_policy at module load.
        from core.wallet.tx_guard import autonomous_max_usd, ceiling_scope
        return float(autonomous_max_usd(*ceiling_scope(None)))
    except Exception:
        logger.warning("DeFi autonomous ceiling unresolved; every live spend asks "
                       "the owner", exc_info=True)
        return 0.0


def x402_autonomous_ceiling_usd() -> float:
    """Per-payment ceiling below which an x402 fetch runs act-and-report.

    Deliberately far smaller than DEFI_AUTONOMOUS_MAX_USD: an x402 payment is
    NOT tx_guard-simulated. Its only bounds are this ceiling, the PolicyGate
    per-tx ceiling, and the rolling daily cap — so the autonomous slice must be
    small enough that a looped drain is bounded by the daily cap long before it
    matters.

    Clamped like the DeFi ceiling: never above the effective per-tx ceiling or
    the signer's ``x402_per_payment_usd`` — a ceiling the payment can never
    reach would be reported as reachable. A failed resolve fails CLOSED to 0.0.
    """
    value = float_env("X402_AUTONOMOUS_MAX_USD", DEFAULT_X402_AUTONOMOUS_MAX_USD)
    try:
        from core.wallet.config import effective_max_per_tx_usd
        from core.wallet.signer_envelope import clamp
        from core.wallet.tx_guard import ceiling_scope
        value = min(value, float(effective_max_per_tx_usd(*ceiling_scope(None))))
        return float(clamp(value, "x402_per_payment_usd"))
    except Exception:
        logger.warning("x402 autonomous ceiling unresolved; every payment asks "
                       "the owner", exc_info=True)
        return 0.0


#: ``simulatable``: the DEFI_SPEND_VERBS whose param model HAS a ``dry_run``
#: field (default True). ⚠️ This is a POSITIVE list, and it is the only way a
#: call can be read as a simulation. Until 2026-09-23 an absent ``dry_run`` key
#: meant "simulate" for EVERY verb — so ``dapp_browser_dapp_connect``, whose
#: ``ConnectParams`` has no ``dry_run`` at all, was ALWAYS a "simulation" and
#: never reached the owner queue (security analysis H03a). A verb missing here
#: fails CLOSED: it is gated as a live spend. `tests/unit/core/test_spend_lane.py`
#: reflects over every tool and fails when this set disagrees with the models.
def _dry_run_verbs():
    return ids_where(simulatable=True)


def is_simulation(action_name: str, params: Dict[str, Any]) -> bool:
    """True when this call cannot broadcast.

    Only a verb in :data:`DRY_RUN_VERBS` can be a simulation. For those, the
    params arrive as ``model_dump(exclude_unset=True)``, so an ABSENT key means
    the model default — ``True`` on every one of them, pinned by the test.
    """
    if action_name not in view(__name__, "DRY_RUN_VERBS"):
        return False
    value = (params or {}).get("dry_run", True)
    return value is True


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
    if action_name in view(__name__, "X402_SPEND_VERBS"):
        return _x402_exemption(params)
    if is_simulation(action_name, params):
        return ("dry run — the guard is consulted but nothing is broadcast")
    if action_name not in view(__name__, "DEFI_SPEND_VERBS"):
        if action_name in view(__name__, "_RISK_REDUCING_VERBS"):
            # A venue cancel (lane none, risk_reducing): it retires a resting
            # order, moves no funds and grants nothing. Making the owner tap to
            # REDUCE exposure is how a book stays exposed. Every other gate
            # (owner-only tool, correspondent, room, refusal taint) still holds.
            if "revoke" in action_name:
                return "revoke — it only reduces delegated authority; it moves no funds"
            return "cancel — retires a resting order; it moves no funds"
        return None

    if not tiered_spend_lane_enabled():
        return None

    if action_name in view(__name__, "_RISK_REDUCING_VERBS"):
        return "revoke — sets the allowance to zero, so it can only reduce risk"

    declared = params.get("max_spend_usd")
    if not isinstance(declared, (int, float)) or isinstance(declared, bool):
        return None
    if declared <= 0:
        return None

    ceiling = autonomous_ceiling_usd()
    if declared > ceiling:
        return None
    if action_name == "dapp_browser_dapp_connect":
        # CR-L19: arming a dapp session authorizes the whole SESSION budget, not
        # one transaction — a $1 per-tx ceiling over a $10,000 session budget is
        # $10,000 of page-authored spend without a tap. Both must fit.
        session = params.get("session_budget_usd")
        if (not isinstance(session, (int, float)) or isinstance(session, bool)
                or session <= 0 or session > ceiling):
            return None
        return (f"declared ceiling ${declared:.4f} and session budget "
                f"${session:.4f} are within the ${ceiling:.2f} autonomous limit")
    return (f"declared ceiling ${declared:.4f} is within the ${ceiling:.2f} "
            f"autonomous limit")


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


# 067 P4 prerequisite: the five verb sets above, built on first read.
__getattr__ = lazy_module_getattr(__name__, {
    "DEFI_SPEND_VERBS": _defi_spend_verbs,
    "ALWAYS_OWNER_APPROVED_VERBS": _always_owner_approved_verbs,
    "_RISK_REDUCING_VERBS": _risk_reducing_verbs,
    "X402_SPEND_VERBS": _x402_spend_verbs,
    "DRY_RUN_VERBS": _dry_run_verbs,
})
