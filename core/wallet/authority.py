"""Operator-wallet identity checks, independent of turn origin and spend caps.

An ordinary authenticated customer is not the owner of the process wallet.
This is application authorization, not isolation from arbitrary in-process code.
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


def leaf_refusal(context, verb):
    """A delegated sub-agent/leaf never moves money — it reports back.

    ONE shape for every money verb (bridge, deploy, launch, dapp connect,
    liquidity, the SPL mint): an autonomous goal/cron turn MAY run the verb,
    bounded by the caps, the simulation and the owner queue above the ceiling —
    caps, not taps. What is refused is a LEAF, because a delegated worker that
    can move the treasury has escaped every bound its parent operated under.
    Fails CLOSED on a probe error, then applies :func:`turn_refusal`.
    """
    try:
        if getattr(context, "role", None) == "leaf" or \
                getattr(context, "is_sub_agent", False):
            return (f"refused: a delegated sub-agent may not {verb} — report back "
                    f"and let the parent run it. Nothing was broadcast.")
    except Exception:
        return "refused: sub-agent probe failed; failing closed."
    return turn_refusal(context)


def spend_pause_refusal():
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
    from core.wallet import tx_guard
    try:
        refusal = pause_refusal_text("spend", what="spending")
        if refusal:
            return refusal
        if tx_guard._halted():
            return pause_refusal_text("dispatch", what="spending", force=True)
    except Exception as exc:
        return f"refused: pause probe failed ({exc}); failing closed."
    return None


def money_tool(tool_id):
    from core.tool_capabilities import ids_with
    name = str(tool_id)
    if name.startswith('mcp:'):
        name = name[4:]
    return name in ids_with('money')


def money_action(name):
    from core.tool_capabilities import TOOL_CAPABILITIES
    # Longest match keeps polymarket_data separate from polymarket.
    matches = [t for t in TOOL_CAPABILITIES if name == t or name.startswith(t + '_')]
    return bool(matches) and money_tool(max(matches, key=len))
