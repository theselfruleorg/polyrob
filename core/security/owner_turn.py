"""The three clauses a "put something in the world" verb states directly.

``publish`` and ``app_service`` need no HOST capability, so they carry no
compute posture — but neither is something a background re-entry may do.
``compute_posture_allows(ctx, 0)`` cannot express that (level 0 is an
unconditional pass by contract), so both verbs stated the same three clauses
by hand — owner tenant, not a leaf/sub-agent, not a forged turn — and the
two copies had already begun to differ in wording. This is the ONE statement;
each verb supplies its own name and the phrase for what it would do.

The forged-turn kinds come from the same SSOT the posture gate uses
(``core.security.forged_turns``), so a new forged kind is refused here too
without a second edit. Fail-CLOSED: any fault in resolution refuses.
"""
from __future__ import annotations

from typing import Any, Optional


def owner_turn_refusal(execution_context: Any, *, verb: str, does: str,
                       public: str) -> Optional[str]:
    """Non-None -> refuse, with this message.

    *does* completes "a delegated sub-agent never …" (e.g. ``"chooses what
    the world sees"``); *public* completes "only the owner tenant may …"
    (e.g. ``"put something at a public URL"``).
    """
    if execution_context is None:
        return f"{verb} requires an execution context"
    try:
        if getattr(execution_context, "is_sub_agent", False):
            return f"{verb} denied: a delegated sub-agent never {does}"
        if getattr(execution_context, "role", "leaf") != "orchestrator":
            return f"{verb} denied: a leaf agent never {does}"
        metadata = getattr(execution_context, "metadata", None) or {}
        from core.security.forged_turns import FORGED_TURN_KINDS
        if metadata.get("turn_kind") in FORGED_TURN_KINDS:
            return (f"{verb} denied: a self-wake / delegation-result re-entry is not "
                    f"an owner asking to ship")
        from core.config_policy import local_mode_enabled
        from core.instance import is_owner_local_safe, resolve_owner_principal
        if not is_owner_local_safe(
                getattr(execution_context, "user_id", None),
                owner_principal=resolve_owner_principal(),
                local_enabled=local_mode_enabled()):
            return f"{verb} denied: only the owner tenant may {public}"
    except Exception:
        return f"{verb} denied: capability gate unavailable"
    return None
