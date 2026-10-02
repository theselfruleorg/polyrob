"""Operator-wallet identity checks, independent of turn origin and spend caps.

An ordinary authenticated customer is not the owner of the process wallet.
This is application authorization, not isolation from arbitrary in-process code.

067 P1b: moved here from ``core/wallet/authority.py`` (now a re-export shim).
These are the PREDICATES; :func:`core.money.authorize.authorize_spend` is the
one composition of them. Tier-0: imports nothing above ``core``.
"""


def owner_refusal(user_id):
    try:
        from core.instance import resolve_owner_principal
        owner = resolve_owner_principal()
        if isinstance(user_id, str) and user_id and user_id == owner:
            return None
    except Exception:
        pass
    return 'operator wallet access requires the bound owner identity'


def network_verb_refusal(verb, user_id, read_verbs):
    """CR-M14: a network caller (an HTTP route, no execution context) running a
    verb that is not in ``read_verbs`` must be the bound owner. The routes also
    bind the caller as the ambient exec identity, so :func:`turn_refusal`
    re-checks the SAME principal inside the verb. Returns the refusal or None."""
    if verb in read_verbs:
        return None
    refusal = owner_refusal(user_id)
    return f"{verb}: {refusal}" if refusal else None


def turn_refusal(context):
    from core.exec_identity import current_exec_identity
    ambient, _ = current_exec_identity()
    # Bare calls remain an internal/local Python interface. Network execution
    # must carry a context; the controller enforces it before every money call.
    if context is None:
        return owner_refusal(ambient) if ambient else None
    uid = getattr(context, 'user_id', None)
    if ambient and ambient != uid:
        return 'wallet execution identity disagrees with the authenticated caller'
    return owner_refusal(uid)


#: Stamped by a caller that acts for a PAGE, not for the turn it was armed in
#: (the dapp wallet bridge): a later page request is never the owner asking,
#: even when the owner armed the session in the owner's own turn.
PAGE_REQUEST_KEY = "page_request"


def owner_direct_turn(context, forged_fn, tool_self=None):
    """True only when THE OWNER is asking, in this turn: a genuine owner chat
    turn, or an owner seat verb (``/send … go``, ``/bridge … go``).

    Owner intent in a genuine owner turn is authorization. The autonomy limits
    — the pause and the autonomous ceiling — bound what the agent does on its
    own; they do not bind the owner acting through it. The hard bounds (per-tx,
    daily, the simulation) bind everyone and are never keyed on this.

    ⚠️ ``context is None`` is NEVER owner-direct. The remote signer re-runs
    ``tx_guard`` with no context (``core/signer/server.py``), and an internal
    caller that forgets its context must stay least-privileged; treating
    ``None`` as the owner would skip the pause and the ceiling for both.
    A page request (``PAGE_REQUEST_KEY``), a leaf/sub-agent, a forged or autonomous turn (``forged_fn``, injected: turn
    origin is a TOOLS-tier fact), a room turn and a non-owner principal all
    answer False. Every probe error answers False.
    """
    if context is None or forged_fn is None:
        return False
    try:
        if (getattr(context, "metadata", None) or {}).get(PAGE_REQUEST_KEY):
            return False
        if getattr(context, "is_sub_agent", False) or \
                getattr(context, "role", "leaf") == "leaf":
            return False
        if forged_fn(context, tool_self):
            return False
        return turn_refusal(context) is None
    except Exception:
        return False


def leaf_refusal(context, verb):
    """A delegated sub-agent/leaf never moves money — it reports back.

    ONE shape for every money verb (bridge, deploy, launch, dapp connect,
    liquidity, the SPL mint): an autonomous goal/cron turn MAY run the verb,
    bounded by the caps, the simulation and the owner queue above the ceiling —
    caps, not taps. What is refused is a LEAF, because a delegated worker that
    can move the treasury has escaped every bound its parent operated under.
    Fails CLOSED on a probe error, then applies :func:`turn_refusal`.
    """
    return delegated_refusal(context, verb) or turn_refusal(context)


def delegated_refusal(context, verb):
    """The leaf half of :func:`leaf_refusal` alone (no principal check)."""
    try:
        if getattr(context, "role", None) == "leaf" or \
                getattr(context, "is_sub_agent", False):
            return (f"refused: a delegated sub-agent may not {verb} — report back "
                    f"and let the parent run it. Nothing was broadcast.")
    except Exception:
        return "refused: sub-agent probe failed; failing closed."
    return None


def spend_pause_refusal(*, entry=False):
    """The 031 owner pause for a money verb. ONE predicate, ONE text, fail-closed.

    Two kinds are consulted because two map differently: ``spend`` is what a
    money verb IS, and ``dispatch`` is what ``AutonomyConfig.autonomy_halted``
    reads (a FACET of the same pause record — there is no separate kill-switch
    to name, which is why the sentence lives in
    ``core.autonomy_control.pause_refusal_text`` beside the record). Either
    denying refuses; ``force=True`` on the halted branch because ``_halted``
    also answers to the legacy env/touch-file facets, which leave the record
    silent, and a refusal must not lose its explanation there.
    """
    from core.autonomy_control import pause_refusal_text
    from core.config_policy import AutonomyConfig
    try:
        refusal = pause_refusal_text("spend", what="spending")
        if refusal:
            return refusal
        # CR-L21: the scoped `/pause trading` (``trade_entry``) binds every
        # ENTRY verb, not only tx_guard and the Solana turn gate. An exit
        # (a sell, a revoke, a claim, an LP removal) passes ``entry=False``.
        if entry:
            refusal = pause_refusal_text("trade_entry", what="opening a position")
            if refusal:
                return refusal
        # The halt facet read directly from the core pause record — the same
        # predicate ``core.wallet.tx_guard._halted`` wraps (067 P1b: the
        # kernel no longer imports the rail to ask it).
        if AutonomyConfig.autonomy_halted():
            return pause_refusal_text("dispatch", what="spending", force=True)
    except Exception as exc:
        return f"refused: pause probe failed ({exc}); failing closed."
    return None


# The classification moved to ``core.money.classify`` (one predicate); these
# names stay importable from here for the ``core.wallet.authority`` shim.
from core.money.classify import money_action, money_tool  # noqa: E402,F401
