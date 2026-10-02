"""``authorize_spend`` — the kernel refusals in ONE fixed order (067 P1b).

Before this, "may this spend happen?" was answered in ~6 places, each composing
its own subset of the :mod:`core.money.authority` predicates. That is how a
verb ended up with the leaf bar and no forged-turn bar (CR-M02). A caller now
DECLARES which bars apply to it (:class:`SpendIntent`) and the kernel runs them
in this order, stopping at the first refusal:

1. **principal** — the caller is the bound owner (``turn_refusal``). With
   ``session_user`` the controller's binding is checked too: the session's
   user must be the owner, a context is required, and the context's user must
   be the session's (``tools/controller/wallet_authority.py``).
2. **leaf** — a delegated sub-agent never spends (``leaf_refusal``). Its
   sentence wins over the principal's when both apply, as it always did.
3. **turn** — a forged or autonomous turn (``forged_fn``, injected: turn origin
   is a TOOLS-tier fact) is refused unless ``autonomous_ok_fn`` admits it as
   the autonomous lane. No context = a direct call = no turn to judge. A
   context with no ``forged_fn`` refuses (fail closed by construction).
4. **pause** — the owner pause (``spend_pause_refusal``), plus the scoped
   ``/pause trading`` when ``intent.entry``. Never on a dry run: a quote
   broadcasts nothing (``spend_lane.py``). Never on a genuine owner turn
   (``owner_direct_turn`` with the injected ``forged_fn``): the pause bounds
   the agent's own work, not the owner acting through it.
5. **ledger** — ``SpendLedger.check`` when the caller asks and the amount is
   known.
6. **lane** — with ``intent.params``, ``spend_lane.spend_exemption`` decides
   act vs owner queue (its thresholds are not duplicated here).

Every probe error refuses. The verdict carries the step that decided, so a
caller that owes its user a rail-specific sentence can map it.

Tier-0: imports nothing above ``core``.
"""
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

ACT = "act"
OWNER_QUEUE = "owner_queue"
REFUSED = "refused"

#: Steps, in order. ``turn_probe`` = the forged-turn detector raised.
STEPS = ("principal", "leaf", "turn", "turn_probe", "pause", "ledger", "lane")

_UNSET: Any = object()


@dataclass(frozen=True)
class SpendIntent:
    """What the caller is about to do, and which bars apply to it."""

    tool: str = ""
    #: The emitted action name (the lane decision keys on it).
    action: str = ""
    #: The leaf sentence's verb phrase ("bridge", "deploy a token").
    what: str = ""
    usd: Optional[float] = None
    #: Opens a position: the scoped ``/pause trading`` binds it too.
    entry: bool = False
    venue: str = ""
    dry_run: bool = False
    idempotency_key: Optional[str] = None
    #: The caller is the bound owner. Off only for a caller that already ran
    #: it in this call (a pause re-check right before signing).
    principal: bool = True
    leaf: bool = True
    turn: bool = False
    pause: bool = True
    check_ledger: bool = False
    #: Given = decide the lane (act vs owner queue) with ``spend_exemption``.
    params: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class Verdict:
    outcome: str
    reason: Optional[str] = None
    step: str = ""
    #: The turn was forged-shaped and admitted as the autonomous lane.
    autonomous: bool = False
    #: The probe error text, when a probe raised.
    detail: str = ""

    @property
    def allowed(self) -> bool:
        return self.outcome == ACT

    @property
    def refused(self) -> bool:
        return self.outcome == REFUSED


FORGED_TEXT = ("refused: a forged/autonomous turn (self-wake, delegation-result, "
               "leaf, or autonomous run) cannot move funds")
NO_DETECTOR_TEXT = ("refused: an agent turn was supplied but no turn-origin detector — "
                    "cannot prove the turn is genuine, so failing closed")
SESSION_CONTEXT_TEXT = "money actions require an authenticated execution context"
SESSION_MISMATCH_TEXT = "money action identity does not match its session"


def _refused(step: str, reason: str, *, detail: str = "") -> Verdict:
    return Verdict(REFUSED, reason, step, detail=detail)


def _principal(context, session_user) -> Optional[str]:
    from core.money.authority import owner_refusal, turn_refusal
    if session_user is _UNSET:
        return None
    error = owner_refusal(session_user)
    if context is None:
        return error or SESSION_CONTEXT_TEXT
    error = error or turn_refusal(context)
    if getattr(context, "user_id", None) != session_user:
        error = SESSION_MISMATCH_TEXT
    return error


def authorize_spend(intent: SpendIntent, context=None, *,
                    forged_fn: Optional[Callable] = None,
                    autonomous_ok_fn: Optional[Callable] = None,
                    tool_self=None,
                    session_user: Any = _UNSET,
                    ledger=None) -> Verdict:
    """The kernel verdict for *intent* on this turn. Never raises."""
    from core.money.authority import delegated_refusal, spend_pause_refusal, turn_refusal

    # -- 1+2. principal, leaf ---------------------------------------------
    # The leaf sentence is checked before the turn principal, exactly as
    # ``leaf_refusal`` always did, so a leaf that is also a stranger still
    # reads "a delegated sub-agent may not ...".
    try:
        if intent.principal:
            error = _principal(context, session_user)
            if error:
                return _refused("principal", error)
        if intent.leaf:
            error = delegated_refusal(context, intent.what or intent.action or "spend")
            if error:
                return _refused("leaf", error)
        if intent.principal:
            error = turn_refusal(context)
            if error:
                return _refused("principal", error)
    except Exception as exc:
        return _refused("principal", f"refused: principal probe failed ({exc}); failing closed",
                        detail=str(exc))

    # -- 3. turn origin ---------------------------------------------------
    autonomous = False
    if intent.turn and context is not None:
        if forged_fn is None:
            return _refused("turn", NO_DETECTOR_TEXT)
        try:
            forged = bool(forged_fn(context, tool_self))
        except Exception as exc:
            return _refused("turn_probe",
                            f"refused: could not prove the turn is genuine ({exc})",
                            detail=str(exc))
        if forged:
            admitted = False
            if autonomous_ok_fn is not None:
                try:
                    admitted = bool(autonomous_ok_fn(context, tool_self))
                except Exception:
                    admitted = False
            if not admitted:
                return _refused("turn", FORGED_TEXT)
            autonomous = True

    # -- 4. owner pause ---------------------------------------------------
    # The pause bounds the agent's OWN work; a genuine owner turn (an owner
    # chat turn or an owner seat verb) is the owner acting, and the pause does
    # not bind it. Needs the injected detector — without it, or with no
    # context, the pause applies (``owner_direct_turn`` answers False).
    from core.money.authority import owner_direct_turn
    if intent.pause and not intent.dry_run \
            and not owner_direct_turn(context, forged_fn, tool_self):
        try:
            refusal = spend_pause_refusal(entry=intent.entry)
        except Exception as exc:
            return _refused("pause", f"refused: pause probe failed ({exc}); failing closed",
                            detail=str(exc))
        if refusal:
            return _refused("pause", refusal)

    # -- 5. ledger --------------------------------------------------------
    if intent.check_ledger and intent.usd is not None:
        try:
            if ledger is None:
                from core.money.hooks import get_spend_ledger
                ledger = get_spend_ledger()
            if ledger is None:
                return _refused("ledger", "refused: no spend ledger is registered; failing closed")
            decision = ledger.check(venue=intent.venue or intent.tool,
                                    amount_usd=intent.usd,
                                    idempotency_key=intent.idempotency_key)
            if decision.allowed is not True:
                return _refused("ledger", f"refused: {decision.reason}")
        except Exception as exc:
            return _refused("ledger", f"refused: spend ledger check failed ({exc}); failing closed",
                            detail=str(exc))

    # -- 6. lane ----------------------------------------------------------
    if intent.params is not None and intent.action:
        try:
            from core.config_policy.spend_lane import spend_exemption
            exemption = spend_exemption(intent.action, dict(intent.params))
        except Exception:
            exemption = None  # the safe answer: keep the owner tap
        if exemption:
            return Verdict(ACT, exemption, "lane", autonomous=autonomous)
        return Verdict(OWNER_QUEUE, "owner approval required", "lane", autonomous=autonomous)

    return Verdict(ACT, None, "", autonomous=autonomous)
