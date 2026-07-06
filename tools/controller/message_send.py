"""Pure-ish send helper for the `message` action: resolve tier, gate, route.
Router is any object with async send_message(chat_id, text, surface_id)."""
import logging
from core.surfaces.outbound_target import resolve_target_tier

logger = logging.getLogger(__name__)


async def perform_message_send(*, router, allowlist, owner_targets, user_id,
                               surface, target, text, action="send", reply_to=None,
                               message_id=None) -> dict:
    tier = resolve_target_tier(surface=surface, target=target, user_id=user_id,
                               allowlist=allowlist, owner_targets=owner_targets)
    if tier == "denied":
        return {"success": False, "tier": "denied", "surface": surface, "target": target,
                "error": ("target not on owner allowlist; ask the owner to run "
                          f"`/allow {surface} {target}` (or `polyrob owner allow {surface} {target}`)")}
    if action not in ("send", "reply"):
        # edit/delete/react are capability-gated and deferred to P2; fail cleanly.
        return {"success": False, "tier": tier, "surface": surface, "target": target,
                "error": f"action '{action}' not supported yet on {surface}"}
    if router is None:
        return {"success": False, "tier": tier, "surface": surface, "target": target,
                "error": "no message_router available (SINGULAR_CHAT_ENABLED off?)"}
    try:
        ok = await router.send_message(chat_id=target, text=text, surface_id=surface)
    except Exception as e:  # fail-open: never crash the loop on a send fault
        logger.error("message send failed: %s", e, exc_info=True)
        return {"success": False, "tier": tier, "surface": surface, "target": target, "error": str(e)}
    return {"success": bool(ok), "tier": tier, "surface": surface, "target": target,
            "error": None if ok else "send returned false"}
