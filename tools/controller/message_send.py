"""Pure-ish send helper for the `message` action: resolve tier, gate, route.
Router is any object with async send_message(chat_id, text, surface_id, media=None) and
an optional sync capabilities(surface_id) lookup (MessageRouter provides both)."""
import logging
import os
from pathlib import Path
from typing import List, Optional, Tuple

from core.surfaces.attachments import (
    _IMAGE_EXTS,  # re-export (legacy import site)
    media_entries_from_paths,
    message_media_max_mb,
    screen_attachment_path,
    validate_media_paths,
)
from core.surfaces.outbound_target import resolve_target_tier

logger = logging.getLogger(__name__)


def _pref_home_dir(container) -> Optional[str]:
    """Preferences-store root for `resolve_outbound_policy`/`resolve_outbound_daily_cap`
    (013 T6) — mirrors `core.surfaces.user_delivery._home_dir_for_container`'s
    data_dir lookup. `None` when there's no container to resolve from at all,
    which makes both resolvers skip the pref layer entirely (env/mode-default
    only) — a container present but config-less still gets the same "data"
    fallback root every other prefs reader uses."""
    if container is None:
        return None
    cfg = getattr(container, "config", None)
    from core.runtime_paths import data_dir_or_home
    return data_dir_or_home(getattr(cfg, "data_dir", None))


def _resolve_session_workspace(session_id: Optional[str], user_id: Optional[str]) -> Optional[str]:
    """Session workspace dir for media-path validation — mirrors the pm() lookup
    pattern used elsewhere (e.g. tools/x402/invoice_tool.py::_resolve_workspace_dir).
    Fail-open to None; the caller rejects media_paths outright when a workspace can't
    be resolved (no session -> nothing to scope 'inside the workspace' against)."""
    if not session_id:
        return None
    try:
        from agents.task.path import pm
        return str(pm().get_workspace_dir(session_id, user_id))
    except Exception:
        return None


def _validate_media_paths(paths: List[str], workspace_dir: Optional[str]) -> Tuple[Optional[List[str]], Optional[str]]:
    """Confinement contract relocated to ``core.surfaces.attachments`` (QW-1)
    so the core delivery rail shares it; this name stays as the import site
    existing callers/tests use."""
    return validate_media_paths(paths, workspace_dir)


def _media_entries_from_paths(paths: List[str]) -> list:
    return media_entries_from_paths(paths)


def _surface_media_out(router, surface_id: str) -> bool:
    """Unknown/uninspectable router -> treat as NOT supporting media (the safe,
    conservative default the 'honest note' relies on)."""
    caps_fn = getattr(router, "capabilities", None)
    if not callable(caps_fn):
        return False
    try:
        caps = caps_fn(surface_id)
    except Exception:
        return False
    return bool(getattr(caps, "media_out", False)) if caps is not None else False


#: Human-friendly aliases an agent naturally types for "the bound owner" — it has
#: no other way to learn the raw chat_id/address, since that's deliberately never
#: surfaced to it. Resolved to the REAL owner_targets[surface] value before tier
#: resolution; live-observed (`target='owner'`) reaching the Telegram API verbatim
#: and failing 'chat not found' under OUTBOUND_POLICY=open (AUTONOMY_MODE=autonomous),
#: where the tier gate no longer catches an unresolved literal target.
_OWNER_ALIASES = {"owner", "the owner", "me"}


def _matches_own_bot_username(router, surface: str, target: str) -> bool:
    """True when `target` (with or without a leading '@') is the surface's OWN
    bot handle. Live-observed (2026-07-19): a goal-completion `message` action
    used its own `@<bot>` Telegram handle as the target instead of 'owner',
    which Telegram rejects outright ("the bot can't send messages to the
    bot") — silently dropping what was meant to be an owner notification. A
    bot can never legitimately be its own message recipient, so this can only
    ever mean "notify my owner"; safe to alias without hardcoding any
    deployment-specific username (resolved from the live surface, not a
    literal string)."""
    get_username = getattr(router, "bot_username", None)
    if not callable(get_username):
        return False
    try:
        own = get_username(surface)
    except Exception:
        return False
    if not own:
        return False
    return target.strip().lstrip("@").lower() == str(own).strip().lstrip("@").lower()


async def perform_message_send(*, router, allowlist, owner_targets, user_id,
                               surface, target, text, action="send", reply_to=None,
                               message_id=None, media_paths=None, session_id=None,
                               container=None) -> dict:
    from core.surfaces.outbound_policy import resolve_outbound_daily_cap, resolve_outbound_policy

    if isinstance(target, str) and (
        target.strip().lower() in _OWNER_ALIASES
        or _matches_own_bot_username(router, surface, target)
    ):
        resolved = (owner_targets or {}).get(surface)
        if resolved:
            target = resolved

    home_dir = _pref_home_dir(container)
    policy, domains = resolve_outbound_policy(user_id or "", surface, home_dir=home_dir)
    tier = resolve_target_tier(surface=surface, target=target, user_id=user_id,
                               allowlist=allowlist, owner_targets=owner_targets,
                               policy=policy, domains=domains)
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

    store = None
    if tier != "owner" and container is not None:
        try:
            store = container.get_service("conversation_store")
        except Exception:
            store = None

    # T6: the open-tier (incl. a domains-match, which also resolves tier="open")
    # daily send is capped tenant+surface-wide, checked BEFORE the seed rail.
    if tier == "open" and store is not None:
        cap = resolve_outbound_daily_cap(user_id or "", home_dir=home_dir)
        try:
            sent_today = store.outbound_count_surface_since(user_id or "", surface, 86400)
        except Exception:
            sent_today = 0  # fail-open: a query fault must never block the send
        if sent_today >= cap:
            return {"success": False, "tier": tier, "surface": surface, "target": target,
                    "error": (f"outbound daily send cap ({cap}) reached for {surface}; "
                              "owner can raise outbound.daily_send_cap")}

    # T6: first-contact MUST be detected before the send — maybe_seed_correspondent
    # only reports the correspondent-registry state (disabled/refused/pending/
    # active), never new-vs-existing; the conversation store's own row (created
    # only by record_outbound/record_inbound) is the reliable "have we ever
    # contacted this address" signal.
    first_contact = False
    if store is not None and tier != "owner":
        try:
            first_contact = store.get(user_id or "", surface, str(target)) is None
        except Exception:
            first_contact = False

    # A1/A2 (2026-07-13 review): the proactive send is the ONLY moment the reply
    # binding can be created — router.send_message uses a synthetic `direct:` key
    # that no surface-level seed can resolve, so a third-party recipient's reply
    # was DENIED at the routing boundary on every surface. Seed BEFORE sending
    # (A5 parity): a cap-refused binding blocks the send; a fault never does.
    seed_state = None
    if tier != "owner" and container is not None:
        try:
            from core.surfaces.seed import maybe_seed_correspondent
            seed_state = maybe_seed_correspondent(
                container, surface=surface, address=str(target),
                session_id=session_id or "", user_id=user_id or "",
                provenance="owner")
        except Exception as e:  # fail-soft: a seed fault must not block the send
            logger.debug("message-send correspondent seed skipped: %s", e)
            seed_state = None
        if seed_state == "refused":
            return {"success": False, "tier": tier, "surface": surface, "target": target,
                    "error": ("correspondent per-day cap reached — reply binding "
                              "refused; message not sent (raise "
                              "CORRESPONDENT_MAX_NEW_PER_DAY or approve pending "
                              "correspondents)")}

    media = None
    note = None
    if media_paths:
        workspace_dir = _resolve_session_workspace(session_id, user_id)
        validated, err = _validate_media_paths(list(media_paths), workspace_dir)
        if err:
            return {"success": False, "tier": tier, "surface": surface, "target": target,
                    "error": f"media rejected: {err}"}
        # QW-1 (2026-07-19): attach-eligibility screen — size cap (the larger
        # MESSAGE_MEDIA_MAX_MB, this is an explicit owner-directed send),
        # secret filename+content, injection scan (resolved HERE — the core
        # module never imports modules.*, layering ratchet). Rejects loudly so
        # the agent can react (vs the completion producer's listed-not-attached).
        try:
            from modules.memory.task.threat_scan import is_suspicious as _scanner
        except ImportError:
            _scanner = None
        for real in validated:
            reason = screen_attachment_path(real, max_mb=message_media_max_mb(),
                                            scanner=_scanner)
            if reason:
                return {"success": False, "tier": tier, "surface": surface, "target": target,
                        "error": f"media rejected: {Path(real).name}: {reason}"}
        if _surface_media_out(router, surface):
            media = _media_entries_from_paths(validated)
        else:
            note = (f"surface {surface} does not support media; sent text only "
                    "— media not delivered")

    try:
        ok = await router.send_message(chat_id=target, text=text, surface_id=surface, media=media)
    except Exception as e:  # fail-open: never crash the loop on a send fault
        logger.error("message send failed: %s", e, exc_info=True)
        return {"success": False, "tier": tier, "surface": surface, "target": target, "error": str(e)}
    # E1 (2026-07-13 review): append the proactive outbound to the durable
    # conversation log (owner targets are not correspondent conversations).
    if ok and tier != "owner" and container is not None:
        try:
            if store is None:
                store = container.get_service("conversation_store")
            if store is not None:
                store.record_outbound(user_id or "", surface, str(target), text,
                                      session_id=session_id or "")
        except Exception as e:
            logger.debug("message-send conversation record skipped: %s", e)

    # T6: first-contact report — AFTER a successful send+record. A blocked or
    # failed send never "made contact", so this only fires on `ok`.
    # Only report for open-tier sends (allowlisted/supervised sends to known
    # correspondents are NOT "open contact" and should not fire this report).
    if ok and first_contact and tier == "open":
        from core.surfaces.outbound_policy import notify_first_contact
        await notify_first_contact(container, user_id, session_id, surface, target)

    result = {"success": bool(ok), "tier": tier, "surface": surface, "target": target,
            "error": None if ok else "send returned false"}
    # Overnight 2026-07-19 finding: an attachment-blind result ("... OK") made
    # the agent retry the same send ~12x and declare BLOCKED — the result must
    # ACKNOWLEDGE what rode the message so success is legible.
    if ok and media:
        result["media_attached"] = [Path(e["path"]).name for e in media]
    if seed_state in ("pending", "active"):
        result["correspondent"] = seed_state
        if seed_state == "pending":
            pending_note = (f"recipient {surface}:{target} is a PENDING correspondent — "
                            "their replies will not route back until the owner runs "
                            f"`polyrob owner approve {surface} {target}`")
            note = f"{note}; {pending_note}" if note else pending_note
    if note:
        result["note"] = note
    return result
