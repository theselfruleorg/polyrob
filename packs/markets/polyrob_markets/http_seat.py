"""The ONE owner seat for the markets HTTP ``/execute`` routes (W5, D3).

Hyperliquid and Polymarket each had an identical ``_owner_gate_and_bind`` and
called the verb directly, past the Controller's hook chain. Both routes now call
:func:`execute_as_seat`:

1. CR-M14: a non-read verb needs the bound owner (403 otherwise); the caller is
   bound as the ambient exec identity so the verb's own ``turn_refusal``
   re-checks the SAME principal.
2. W5: the call runs between the seat stages of the one policy chain
   (``polyrob.sdk.run_seat_action``: the operator denylist, the effect gate and
   record, the refusal-taint gate and record), with the one owner-seat context
   (``polyrob.sdk.owner_seat_context``) — so an HTTP caller cannot bypass them.
   A refusal is a 403 with the chain's sentence.
"""
from fastapi import HTTPException


def owner_gate_and_bind(tool_name: str, user_id: str, read_actions, venue: str):
    """CR-M14: an HTTP ``/execute`` of a trade or mutation verb is an OWNER act.
    Returns the exec-identity token to reset in a ``finally``."""
    from core.exec_identity import set_exec_identity
    from core.wallet.authority import network_verb_refusal
    refusal = network_verb_refusal(tool_name, user_id, read_actions)
    if refusal:
        raise HTTPException(status_code=403, detail=refusal)
    return set_exec_identity(user_id, f"http:{venue}")


async def execute_as_seat(tool, *, tool_id: str, tool_name: str, arguments, user_id: str,
                          read_actions, venue: str):
    """Gate, bind, and run ``tool.execute_action(tool_name, arguments)`` as the
    owner's ``api`` seat. Raises ``HTTPException(403)`` on a refusal; any other
    exception from the verb propagates (the routes map it as before)."""
    from core.exec_identity import reset_exec_identity
    from polyrob.sdk import SeatRefused, owner_seat_context, run_seat_action
    token = owner_gate_and_bind(tool_name, user_id, read_actions, venue)
    try:
        # Task-local user context (CR-M14).
        tool.set_user_context(user_id)
        ctx = owner_seat_context(user_id, seat="api")
        try:
            return await run_seat_action(
                tool_id=tool_id, action=f"{tool_id}_{tool_name}", params=dict(arguments or {}),
                execution_context=ctx,
                call=lambda: tool.execute_action(tool_name, arguments or {}))
        except SeatRefused as refused:
            raise HTTPException(status_code=403, detail=str(refused))
    finally:
        reset_exec_identity(token)


__all__ = ["owner_gate_and_bind", "execute_as_seat"]
