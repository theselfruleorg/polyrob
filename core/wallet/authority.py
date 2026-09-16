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
