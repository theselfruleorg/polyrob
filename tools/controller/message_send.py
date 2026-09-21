"""Pure-ish send helper for the `message` action: resolve tier, gate, route.
Router is any object with async send_message(chat_id, text, surface_id, media=None) and
an optional sync capabilities(surface_id) lookup (MessageRouter provides both)."""
import logging
import os
from pathlib import Path
from typing import List, Optional, Tuple

from core.surfaces.attachments import (
    _IMAGE_EXTS,  # re-export (legacy import site)
    is_injection_reason,
    media_entries_from_paths,
    message_media_max_mb,
    screen_attachment_path,
    validate_media_paths,
)
from core.surfaces.outbound_target import (
    is_bot_username,
    normalize_surface_target,
    resolve_target_tier,
    wrong_surface_target_reason,
)
from core.surfaces.room_keys import is_room_target

logger = logging.getLogger(__name__)


def _pref_home_dir(container) -> Optional[str]:
    """Preferences-store root for `resolve_outbound_policy`/`resolve_outbound_daily_cap`
    (013 T6) — the IDENTITY axis, same as `user_delivery._home_dir_for_container`.

    `None` when there's no container at all, which makes both resolvers skip the
    pref layer entirely (env/mode-default only). With a container it is the data
    home: `config.data_dir` is `<data_home>/data` on a server, a shadow no
    preference writer ever writes to (2026-09-15 review, C10)."""
    if container is None:
        return None
    from core.runtime_paths import prefs_home_dir
    return prefs_home_dir()


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


#: 031 owner pause -> the ``core.autonomy_control`` kind a send is judged by.
#: The distinction is derivable at this seam from the resolved TARGET TIER: an
#: autonomous ping to the owner is a lifecycle ping (``/pause pings``); a send to
#: anyone else is an outward post (``/pause social``). Both are denied by a full
#: ``/pause``. No new scope names — these are rows of ``KIND_SCOPES``.
_PAUSE_KIND_BY_TIER = {"owner": "lifecycle_ping"}
_PAUSE_DEFAULT_KIND = "social_post"
#: the scope an owner would resume to unblock each kind (for the refusal text)
_PAUSE_SCOPE_HINT = {"lifecycle_ping": "pings", "social_post": "social"}


def message_pause_refusal(execution_context, controller, *, tier: str) -> Optional[str]:
    """031 coupling: an autonomous/forged turn may not send while the owner paused.

    ``perform_message_send`` had NO pause probe, so `/pause pings`, `/pause social`
    and even `/pause all` left the dominant autonomous outbound path — a goal or
    cron session calling the `message` action — wide open. This is that probe, and
    it covers every surface the action can address.

    Polarity is the same as every money/social gate: an OWNER-initiated turn is
    never gated (asking the agent to send IS the owner being in the loop), and a
    context-less programmatic/CLI call is not gated either (mirrors
    ``TwitterTool._pause_block``). Returns a refusal string, or None to proceed.
    Fail-OPEN on a probe error; ``allows`` itself already fails CLOSED on an
    unreadable pause record.
    """
    if execution_context is None:
        return None  # owner-direct / CLI / programmatic call
    try:
        from tools.controller.turn_origin import _is_forged_or_autonomous_turn
        if not _is_forged_or_autonomous_turn(execution_context, controller):
            return None
        from core.autonomy_control import allows
        kind = _PAUSE_KIND_BY_TIER.get(tier, _PAUSE_DEFAULT_KIND)
        dec = allows(kind)
        if dec.allowed:
            return None
    except Exception:
        logger.debug("message pause probe failed (fail-open)", exc_info=True)
        return None
    return (f"message blocked: autonomy is {dec.reason}. Resume with "
            f"`/resume {_PAUSE_SCOPE_HINT[kind]}` (or `/resume`) when you want it back.")


async def perform_message_send(*, router, allowlist, owner_targets, user_id,
                               surface, target, text, action="send", reply_to=None,
                               message_id=None, media_paths=None, session_id=None,
                               container=None, execution_context=None,
                               controller=None) -> dict:
    from core.surfaces.outbound_policy import resolve_outbound_daily_cap, resolve_outbound_policy

    if isinstance(target, str) and (
        target.strip().lower() in _OWNER_ALIASES
        or _matches_own_bot_username(router, surface, target)
    ):
        resolved = (owner_targets or {}).get(surface)
        if resolved:
            target = resolved

    # AFTER owner-alias resolution (so 'owner' never becomes '@owner'):
    # compute the API-shaped form of an agent-typed telegram target — t.me
    # links and bare usernames reached the Bot API verbatim and failed
    # "chat not found" (2026-08-15..16). Tier/allowlist/store matching below
    # deliberately keeps the RAW target (owner-authored allowlist entries are
    # matched byte-exact); only the actual send uses the normalized form.
    send_target = normalize_surface_target(surface, target)
    # C8 (2026-09-15): a target of the wrong SHAPE for its surface can never be
    # delivered. Prod marked `telegram / rob@theselfrule.org` dead after handing
    # an email address to the Bot API. Refuse with the surface that WOULD work,
    # before the send is spent and the address is durably marked dead.
    wrong_shape = wrong_surface_target_reason(surface, send_target)
    if wrong_shape:
        return {"success": False, "tier": None, "surface": surface, "target": target,
                "error": wrong_shape}
    if is_bot_username(surface, send_target):
        return {"success": False, "tier": None, "surface": surface, "target": target,
                "error": (f"{send_target} is a bot account — Telegram forbids bot→bot "
                          "messages, the send can never succeed. If you meant the "
                          "owner, use target='owner'; if this is a channel whose "
                          "@username ends in 'bot', use its numeric -100… chat id.")}

    home_dir = _pref_home_dir(container)
    policy, domains = resolve_outbound_policy(user_id or "", surface, home_dir=home_dir)
    tier = resolve_target_tier(surface=surface, target=target, user_id=user_id,
                               allowlist=allowlist, owner_targets=owner_targets,
                               policy=policy, domains=domains)
    # 044 T21: a ROOM is neither the owner nor a third party. It is a chat the
    # OWNER put the agent in, so the correspondent rail below (seeding, the
    # per-day new-contact cap) and the open-tier daily cap are the wrong bounds —
    # its bound is the room's own hourly reply cap, the SAME `RoomCaps` gate
    # `MessageRouter.publish` applies to a live room reply. Resolved HERE,
    # immediately after the tier and before the `denied` exit, because a room's
    # chat id was never on the OUTBOUND allowlist: the ladder calls it `denied`
    # and the agent could not post into its own room at all.
    room = tier != "owner" and is_room_target(container, surface, target)
    if room:
        tier = "room"
    if tier == "denied":
        return {"success": False, "tier": "denied", "surface": surface, "target": target,
                "error": ("target not on owner allowlist; ask the owner to run "
                          f"`/allow {surface} {target}` (or `polyrob owner allow {surface} {target}`)")}
    # 031 owner pause — BEFORE the daily-cap/seed/send rail, so a paused send
    # never creates a correspondent binding or burns a cap slot either. The
    # refusal is a real ActionResult error downstream (_message_action_result),
    # so it lands in the step ledger + evidence pack, never silently swallowed.
    pause_refusal = message_pause_refusal(execution_context, controller, tier=tier)
    if pause_refusal is not None:
        logger.info("message send refused by owner pause: %s:%s (%s)",
                    surface, target, pause_refusal)
        return {"success": False, "tier": tier, "surface": surface, "target": target,
                "error": pause_refusal}
    if action not in ("send", "reply"):
        # edit/delete/react are capability-gated and deferred to P2; fail cleanly.
        return {"success": False, "tier": tier, "surface": surface, "target": target,
                "error": f"action '{action}' not supported yet on {surface}"}
    if router is None:
        return {"success": False, "tier": tier, "surface": surface, "target": target,
                "error": "no message_router available (SINGULAR_CHAT_ENABLED off?)"}

    # 044 T21: the room's own hourly cap, the SAME bound the live reply path
    # applies — so a post cannot dodge it by going through the `message` tool.
    # Checked BEFORE the send; `record_reply` only fires on a SUCCESSFUL one, so
    # a failed post never consumes the room's budget for nothing.
    room_caps = None
    if room:
        try:
            room_caps = container.get_service("room_caps") if container else None
        except Exception as e:
            logger.debug("room caps lookup failed: %s", e)
            room_caps = None
        if room_caps is not None:
            ok_room, why = room_caps.may_reply(surface, str(target))
            if not ok_room:
                return {"success": False, "tier": "room", "surface": surface,
                        "target": target, "error": why}
        # 044 I6: the OTHER two room-delivery rules. `MessageRouter.publish`
        # applies them to a live room REPLY, but a post through the `message`
        # tool goes straight to `router.send_message` and bypassed both — so the
        # one verb an agent can aim at an arbitrary chat was the one that skipped
        # the public-audience scrub. ONE audience, one set of rules.
        if (text or "").strip().upper() == "[SILENT]":
            # The agent's "nothing here needs an answer". In a room that must
            # cost NO message, or a judgement of silence becomes a public
            # non-sequitur. EXACT match only, as in publish (044 §4.4).
            logger.info("room post suppressed: [SILENT] (%s:%s)", surface, target)
            return {"success": True, "tier": "room", "surface": surface,
                    "target": target, "suppressed": True,
                    "note": "[SILENT] — nothing was posted to the room"}
        from core.secret_scrub import scrub_secret_shapes
        _safe = scrub_secret_shapes(text or "")
        if _safe != text:
            logger.warning("room post: redacted a secret shape before delivery")
            text = _safe

    # 2026-09-15: the owner reads this on a phone. `core.owner_remedy` has known
    # since 2026-09-08 which owner actions are real and which are a shell
    # command, and exactly ONE producer (the goal escalation) consulted it — so
    # the agent's own `message` tool, its commonest route to the owner, sent
    # invented verbs and `polyrob …` instructions unchecked. We never delete the
    # agent's prose (it is usually right about the WHAT and wrong only about the
    # remedy); we append the correction and let the owner see both. Fail-open.
    if tier == "owner" and text:
        try:
            from core.owner_remedy import correction_line, shell_free_correction
            fixups = correction_line(text) + shell_free_correction(text)
            if fixups:
                logger.info("owner message carried unreachable actions; corrected inline")
                text = text + fixups
        except Exception:
            logger.debug("owner remedy check skipped", exc_info=True)

    store = None
    if tier not in ("owner", "room") and container is not None:
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
    if tier not in ("owner", "room") and container is not None:
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
                # I4: `"threat scan" in reason` also matched the scan-ERROR
                # reason, so a crashing scanner reported an attack.
                # I5: the screened thing is a workspace FILE, not an inbound
                # turn — `origin="sender"` sent an owner investigating a
                # hostile chat message that does not exist.
                if is_injection_reason(reason):
                    from core.security.threat_report import report_threat
                    report_threat("file", source="message_send",
                                  user_id=user_id, session_id=session_id,
                                  detail=Path(real).name)
                return {"success": False, "tier": tier, "surface": surface, "target": target,
                        "error": f"media rejected: {Path(real).name}: {reason}"}
        if _surface_media_out(router, surface):
            media = _media_entries_from_paths(validated)
        else:
            note = (f"surface {surface} does not support media; sent text only "
                    "— media not delivered")

    try:
        ok = await router.send_message(chat_id=send_target, text=text, surface_id=surface, media=media)
    except Exception as e:  # fail-open: never crash the loop on a send fault
        logger.error("message send failed: %s", e, exc_info=True)
        return {"success": False, "tier": tier, "surface": surface, "target": target, "error": str(e)}
    # D24 (2026-09-21 interface audit): this IS the turn's reply when it went to
    # the owner, so record it. `send_message` has always marked the turn; the
    # `message` tool — the verb the agent reaches for whenever a file rides
    # along — never did, so an UNBOUND seat (raw API, `chat_once`, `/v1`) fell
    # back to scanning history, found `done()`'s "✅ Task Complete\n\n<recap>"
    # as the last AIMessage, and returned a third-person recap as the answer.
    if ok and tier == "owner":
        try:
            from core.surfaces.turn_reply import mark_reply_published
            mark_reply_published(getattr(controller, "orchestrator", None), text)
        except Exception as e:
            logger.debug("message send: reply-record skipped: %s", e)
    # E1 (2026-07-13 review): append the proactive outbound to the durable
    # conversation log (owner targets are not correspondent conversations —
    # this record is for the seed/first-contact/daily-cap machinery above,
    # which owner sends are deliberately exempt from).
    # 044 T21: the room's hourly budget is spent only by a post that LANDED.
    if ok and room and room_caps is not None:
        try:
            room_caps.record_reply(surface, str(target))
        except Exception as e:
            logger.debug("room cap record skipped: %s", e)
    if ok and tier not in ("owner", "room") and container is not None:
        try:
            if store is None:
                store = container.get_service("conversation_store")
            if store is not None:
                # C8: the store's own _norm_addr collapses `x` / `@x` / `t.me/x`
                # into one key on read AND write, so the raw target is correct
                # here and a reply that arrives under any spelling still resolves.
                store.record_outbound(user_id or "", surface, str(target), text,
                                      session_id=session_id or "")
        except Exception as e:
            logger.debug("message-send conversation record skipped: %s", e)
    # 2026-08-27 dedup-guard fix: an owner send still needs SOME durable
    # record — `_autonomous_owner_resend_cooldown_refusal` (turn_origin.py)
    # reads this SAME store to refuse a repeat autonomous send within the
    # cooldown window. Without this, that guard could never see a real send
    # (the block above never fires for tier=="owner"), so it silently never
    # gated anything — confirmed live 2026-08-27: a 5th duplicate owner-ask
    # send went out 11 minutes AFTER the cooldown guard was deployed. `target`
    # here is already the RESOLVED owner address (the alias resolution above
    # ran before tier was computed), so this never collides with a literal
    # 'owner'-keyed row from elsewhere.
    elif ok and tier == "owner" and container is not None:
        try:
            owner_store = container.get_service("conversation_store")
            if owner_store is not None:
                owner_store.record_outbound(user_id or "", surface, str(target), text,
                                            session_id=session_id or "")
        except Exception as e:
            logger.debug("owner-send conversation record skipped: %s", e)
        _record_owner_send_on_the_rail(user_id, session_id, surface, text)

    # T6: first-contact report — AFTER a successful send+record. A blocked or
    # failed send never "made contact", so this only fires on `ok`.
    # Only report for open-tier sends (allowlisted/supervised sends to known
    # correspondents are NOT "open contact" and should not fire this report).
    if ok and first_contact and tier == "open":
        from core.surfaces.outbound_policy import notify_first_contact
        await notify_first_contact(container, user_id, session_id, surface, target)

    result = {"success": bool(ok), "tier": tier, "surface": surface, "target": target,
            "error": None if ok else "send returned false"}
    # 057 WS-E: the proof rule travels WITH the receipt. Without it the agent
    # went looking for its own channel post to "confirm" a send Telegram will
    # never echo back, and then reported a delivered post as unconfirmed.
    # Rendered from the ONE table (core/rails/verification.py); an unknown rail
    # renders nothing rather than inventing a rule.
    if ok:
        try:
            from core.rails.verification import rail_for_message, verification_line
            _rail = rail_for_message(surface, is_room=bool(room))
            _line = verification_line(_rail) if _rail else ""
            if _line:
                result["verification"] = _line
        except Exception as e:
            logger.debug("verification line skipped: %s", e)
    if send_target != target:
        result["sent_as"] = send_target  # e.g. 't.me/x' delivered as '@x'
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


def _record_owner_send_on_the_rail(user_id, session_id, surface: str,
                                  text: str) -> None:
    """Book an owner-tier `message`-tool send into the delivery rail's ledger.

    2026-09-15 prod review, C5: this path never touched
    ``core.surfaces.user_delivery``, so a `message`-tool send to the owner was
    invisible to the shared daily cap and hourly rate limit that every OTHER
    owner-bound producer is measured against. Two rails, one owner, no shared
    accounting — so the cap protected the owner from the framework's chatter
    while this path stayed unmetered.

    This RECORDS, it does not gate: the cooldown above plus the outbound policy
    remain this path's own guard. Recording is what makes the rail's window
    honest, and what lets a future unification gate here without inventing a
    second budget. Fail-open and silent — an unrecordable send is still sent.

    ⚠️ D20 (2026-09-21 interface audit): the row rides the ``exempt`` LANE. It
    used to be ``normal``, so a path the daily cap cannot deny was spending the
    cap that only OTHER producers can be denied for — the same shape C1 removed
    for the critical lane. A lane that cannot be denied must not be able to
    deny others. The row is still written, so the window stays honest about
    what the owner received; it is simply not counted (``_budgeted``).
    """
    try:
        from core.event_kinds import USER_DELIVERY
        from core.event_log import event_log_enabled, get_event_log
        from core.surfaces.user_delivery import PRIORITY_EXEMPT, content_hash
        if not event_log_enabled():
            return
        body = (text or "").strip()
        get_event_log().record(
            USER_DELIVERY, user_id=str(user_id or ""),
            session_id=str(session_id or ""), source="message_tool",
            attrs={"outcome": "sent", "lane": PRIORITY_EXEMPT, "surface": surface,
                   "content_hash": content_hash(body), "text": body[:500]})
    except Exception:
        logger.debug("owner-send rail record skipped", exc_info=True)


#: Surface order used when the model omits `surface`: the owner's primary chat
#: surface first, then the rest of the owner-address contract.
_OWNER_SURFACE_ORDER = ("telegram", "email", "slack", "discord", "signal", "whatsapp", "x")


def resolve_message_defaults(surface, target, owner_targets) -> tuple:
	"""Fill an omitted `surface`/`target` with the owner's primary address.

	Prod 2026-08-24..28: GLM-5 called `message(text=…, media_paths=[…])` 24 times
	without `surface`/`target` — always the final "notify the owner" step of a
	finished goal. Both fields were required, so validation failed, the executor
	reported it as "action does not exist", counted the step as empty, and two
	in a row tripped the thinking-loop escalation. The deliverable existed; the
	owner was never told. An omitted target means the owner; an omitted surface
	means whichever owner surface is bound (telegram first).
	"""
	tgt = (target or "").strip() or "owner"
	sfc = (surface or "").strip()
	if not sfc:
		targets = owner_targets or {}
		for sid in _OWNER_SURFACE_ORDER:
			if targets.get(sid):
				sfc = sid
				break
		else:
			sfc = next(iter(targets), None) or "telegram"
	return sfc, tgt


def prepare_message_targets(container, user_id: str, surface, target) -> tuple:
	"""``(owner_targets, surface, target)`` for one `message` call: the per-surface
	owner addresses plus the (surface, target) the call effectively addresses
	after owner defaults (see ``resolve_message_defaults``)."""
	owner_targets = build_owner_targets(container, user_id)
	sfc, tgt = resolve_message_defaults(surface, target, owner_targets)
	return owner_targets, sfc, tgt


def build_owner_targets(container, user_id: str) -> dict:
	"""Per-surface owner addresses for ``message(target="owner")`` (030 WS-B1/D7).

	telegram/email keep their canonical resolvers; every other surface resolves
	through the owner-address contract, so a deploy that configured
	OWNER_DISCORD_ID stops denying. Fail-open per surface.
	"""
	import os as _os

	from core.instance import resolve_owner_email, resolve_owner_telegram_id
	targets: dict = {}
	tid = resolve_owner_telegram_id(_os.environ)
	if tid:
		targets["telegram"] = str(tid)
	oem = resolve_owner_email(_os.environ)
	if oem:
		targets["email"] = oem
	try:
		from core.surfaces.owner_address import owner_address
		for _sid in ("slack", "discord", "signal", "whatsapp", "x"):
			if _sid not in targets:
				_addr = owner_address(container, _sid, user_id)
				if _addr:
					targets[_sid] = str(_addr)
	except Exception:
		pass
	return targets
