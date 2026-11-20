"""What every owner money verb shares: the owner-seat context, the policy-chain
call, the caps line and the owner's wording of a rail report."""
from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

#: The rail's sentence for the AGENT ("call me again live"). The owner's way to
#: act is the `go` line the verb appends, so the sentence is dropped.
_AGENT_RERUN = re.compile(r"\s*Re-run with dry_run=false to [a-z]+\.")


def owner_ctx(user_id: str, *, seat: str = "telegram", verb: str = "send"):
    """A genuine owner seat turn. Each call is its own turn: the replay keys
    hash the turn id, and with none the same send to the same address was
    'replay blocked' forever on Solana (validation, 2026-09-27)."""
    import uuid
    from core.security.turn_facts import owner_seat_context
    return owner_seat_context(user_id, seat=seat, turn_id=f"{verb}-{uuid.uuid4().hex}")


def tool_id_of(tool: Any, default: str) -> str:
    return str(getattr(tool, "tool_id", None) or getattr(tool, "name", None) or default)


def facts(result: Any):
    """The typed facts a rail attached (``BaseTool._ar(metadata=…)``), or None."""
    meta = getattr(result, "metadata", None)
    return meta if isinstance(meta, dict) else None


async def run_tool(tool: Any, *, tool_id: str, verb: str, params: Any, ctx: Any,
                   call: Callable[[], Awaitable[Any]], as_owner: bool = False):
    """Run one owner-seat tool call between the seat stages of the one policy
    chain. *verb* is the tool METHOD; the action key is ``core.action_names``'
    rule. A refusal comes back as the verb's own error result, never a raise.

    *as_owner*: run the call under the owner's exec identity (a read that
    resolves the tenant's own data, as ``/check`` and ``/lp`` do)."""
    from tools.controller.policy_chain import SeatRefused, run_seat_action
    tid = tool_id_of(tool, tool_id)
    try:
        values = params.model_dump() if hasattr(params, "model_dump") else dict(params or {})
    except Exception:
        values = {}
    token = None
    if as_owner:
        from core.exec_identity import set_exec_identity
        token = set_exec_identity(str(getattr(ctx, "user_id", "") or ""), None)
    try:
        from core.action_names import namespaced_action_name
        return await run_seat_action(tool_id=tid, action=namespaced_action_name(tid, verb),
                                     params=values,
                                     execution_context=ctx, call=call)
    except SeatRefused as refused:
        from types import SimpleNamespace
        return SimpleNamespace(error=str(refused), extracted_content=None, metadata=None)
    finally:
        if token is not None:
            from core.exec_identity import reset_exec_identity
            reset_exec_identity(token)


def owner_words(text: str) -> str:
    return _AGENT_RERUN.sub("", text or "")


def render(result: Any, *, what: str = "send") -> str:
    if getattr(result, "error", None):
        return f"❌ {result.error}"
    body = getattr(result, "extracted_content", None)
    if not body:
        return (f"The {what} returned neither an error nor a report. That is a bug — "
                "do NOT retry until it is understood; assume nothing about what "
                "happened to the funds.")
    return body


def caps_line(tool: Any) -> str:
    """The hard caps this act must fit, BEFORE it runs. Never a comfortable
    zero: an unreadable cap says so."""
    try:
        wallet = tool._get_wallet()
        gate = wallet.policy if wallet is not None else None
    except Exception:
        gate = None
    if gate is None:
        return "  caps:  unavailable (wallet not readable)\n"
    parts = []
    try:
        parts.append(f"per transaction ${float(gate.per_tx_cap_usd):,.2f}")
    except Exception:
        parts.append("per transaction unavailable")
    try:
        from core.wallet.tx_notify import caps_from_gate
        used, limit = caps_from_gate(gate)
        if limit is None:
            parts.append("daily: no cap set")
        else:
            parts.append(f"daily ${used:,.2f} used of ${limit:,.2f} "
                         f"(${max(0.0, limit - used):,.2f} left)")
    except Exception:
        parts.append("daily unavailable")
    line = "  caps:  " + "; ".join(parts) + "\n"
    try:
        from core.signer import MODE_LOCAL, signer_mode
        if signer_mode() != MODE_LOCAL:
            line += ("  signer: a separate signer also holds its own caps and "
                     "may refuse a send these allow\n")
    except Exception:
        pass
    return line


__all__ = ["caps_line", "facts", "owner_ctx", "owner_words", "render", "run_tool",
           "tool_id_of"]
