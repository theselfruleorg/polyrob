"""The `room_moderate` action — the agent MODERATES an allowlisted room.

Owner rail 2026-10-05: "it should have mute and ban and other exposed as
actions not just read and send messages". Rob could read The Public Den
(`room_read`) and post in it (`message`), and told the owner "I can't ban — I
have no moderation tool" while a spammer posted a promo pitch. The transport
already existed: the owner's own `/mute` `/ban` in the room run through the ONE
adapter (`surfaces/telegram/room_moderator.py`, container service
``room_moderator``). This action is the agent's seat on that same adapter.

⚠️ Same protections as the owner's free path, never fewer: the target is
refused if it is the owner, a live chat administrator/creator, a `group_roles`
admin, or unreadable (`room_actions._target_protection`, fail-CLOSED). The bot
must hold the Telegram right (`can_restrict_members` / `can_delete_messages`);
an unreadable rights set is an empty one.

⚠️ Allowlisted rooms only, matched by the ONE selector rule
(`room_read_action.room_matches`).

⚠️ Never from a room turn (`room_denied`) and never from a correspondent-tainted
session (`correspondent_blocked`) — rows in `core/verb_policy_rows.py`. A member
line in a room must not be able to talk the agent into banning another member;
the agent moderates from its owner/private and autonomous sessions, reading the
room with `room_read`.

⚠️ Layering: `tools/` never imports `surfaces/`. The adapter is reached as a
container service, duck-typed.

⚠️ Registry-closure landmine: NO `from __future__ import annotations` here.
"""
import logging
import time
from dataclasses import dataclass
from typing import List, Optional

from pydantic import BaseModel, Field

from agents.task.agent.views import ActionResult

logger = logging.getLogger(__name__)

_VERBS = ("mute", "unmute", "ban", "unban", "delete")
_DEFAULT_DURATION = {"mute": "1h", "ban": "30d"}
#: Telegram refuses to delete a message older than 48 h.
_DELETE_WINDOW_SEC = 48 * 3600
_MAX_DELETE = 50
_SCAN_ROWS = 2000


@dataclass(frozen=True)
class _Row:
    """The offer shape the adapter and `room_actions.receipt` read."""
    surface: str
    chat_id: str
    verb: str
    target_user_id: str
    target_name: str
    duration_sec: int


def _out(text: str) -> ActionResult:
    return ActionResult(extracted_content=f"room_moderate: {text}",
                        include_in_memory=True)


def resolve_target(rows, target: str, *, allow_unseen: bool = False):
    """``(user_id, display_name)`` or ``(None, reason)`` from the room ledger.

    Matches the sender id exactly, or the sender name with or without ``@``,
    case-insensitive. A bare number with no ledger row is taken as a user id.
    Ambiguity refuses — a ban must never land on the nearest name.
    """
    want = (target or "").strip()
    if not want:
        return None, "name the target (user id or @name from room_read)."
    bare = want.lstrip("@").lower()
    hits = {}
    for r in rows:
        # Only the agent's OWN rows are skipped. Another bot (a spam bot) is a
        # valid target; the live-status probe still protects an admin bot.
        if not r.sender_id or r.sender_id == "agent":
            continue
        name = (r.sender_name or "").strip()
        if want == r.sender_id or (name and name.lstrip("@").lower() == bare):
            hits[r.sender_id] = name or r.sender_id
    if len(hits) == 1:
        uid, name = next(iter(hits.items()))
        return uid, name
    if len(hits) > 1:
        listed = ", ".join(f"{n} (uid={u})" for u, n in hits.items())
        return None, f"'{want}' matches more than one member: {listed}. Use the uid."
    # ⚠️ A uid with no ledger row is accepted ONLY to LIFT a mute/ban (the
    # member may have aged out of the ledger). A mute/ban/delete must name
    # someone who actually posted here, so a planted line cannot aim one at an
    # arbitrary account.
    if allow_unseen and bare.lstrip("-").isdigit():
        return want.lstrip("@"), want.lstrip("@")
    return None, (f"no member named '{want}' in this room's ledger. Read the "
                  f"room with room_read and pass the uid.")


def _turn_tainted(controller, execution_context) -> bool:
    """True when this turn has read third-party text (a room ledger, a page, a
    mail) or a correspondent's message. Fail toward tainted on a fault."""
    try:
        meta = getattr(execution_context, "metadata", None) or {}
        if isinstance(meta, dict) and meta.get("untrusted_read"):
            return True
        orch = getattr(controller, "orchestrator", None)
        return bool(getattr(orch, "_untrusted_read", False) is True
                    or getattr(orch, "_correspondent_tainted", False) is True)
    except Exception:
        return True


def _job_names_room(task: str, chat_id: str, title: str = "") -> bool:
    """The owner's job text names this room: its chat id, or its title (the
    owner writes "moderate The Public Den", not "-1002125904710")."""
    if chat_id and chat_id in task:
        return True
    t = (title or "").strip().lower()
    return len(t) >= 4 and t in task.lower()


async def _moderation_authority(controller, execution_context, chat_id: str, verb: str,
                                params, *, autonomous: bool,
                                title: str = "") -> Optional[str]:
    """CHAT-5: whose decision this moderation is. Returns a refusal, or None.

    - An AUTONOMOUS run moderates only under a standing OWNER-authored cron
      job whose text names this room (the owner wrote the rule; the run applies
      it). An agent-authored job, a goal run or a job for another room may not.
    - An interactive turn that has read third-party text (the room itself,
      through ``room_read``) may have been steered by it: the owner confirms
      THIS action with one tap (the durable owner queue, Telegram /approve).
    - An untainted owner turn is the owner's own instruction.
    """
    if autonomous:
        from agents.task.goals.autonomy_marker import owner_job_task_for_session
        task = owner_job_task_for_session(getattr(execution_context, "session_id", None))
        if task and _job_names_room(task, chat_id, title):
            return None
        return ("moderation from an autonomous run needs a standing owner-authored "
                "job that names this room; nothing was done.")
    if not _turn_tainted(controller, execution_context):
        return None
    try:
        import asyncio
        import tools.controller.approval_queue  # noqa: F401  # registers owner_queue
        from tools.controller.approval import (
            approval_wait_timeout_sec, get_approval_provider_or_deny)
        provider = get_approval_provider_or_deny(
            "owner_queue", user_id=getattr(execution_context, "user_id", None))
        if getattr(provider, "decides_as_owner", False) is not True:
            return ("this turn read third-party text, so a moderation needs the "
                    "owner's tap and no owner queue is available; nothing was done.")
        pdict = params.model_dump() if hasattr(params, "model_dump") else dict(params or {})
        approved = await asyncio.wait_for(
            provider.request("room_moderate", pdict, execution_context),
            timeout=approval_wait_timeout_sec("owner_queue"))
    except Exception as e:
        logger.warning("room_moderate: owner approval failed (%s)", e)
        approved = False
    if approved is not True:
        return (f"{verb} not approved by the owner (this turn read third-party text, "
                f"so each moderation needs the owner's tap); nothing was done.")
    return None


def register_room_moderate_action(controller) -> None:
    """Register `room_moderate`. Rides `GROUP_CHAT_ENABLED`, like `room_read`."""
    from core.surfaces.config import SurfaceConfig
    if not SurfaceConfig.group_chat_enabled():
        return

    class RoomModerateAction(BaseModel):
        room: str = Field(
            description=("The allowlisted room: chat id, 'surface:chat_id', or "
                         "a distinctive fragment of its name or label."))
        action: str = Field(
            description="One of: mute, unmute, ban, unban, delete.")
        target: Optional[str] = Field(
            default=None,
            description=("The member: their uid (from room_read's {uid=…}) or "
                         "@name. Required for mute/unmute/ban/unban; for "
                         "delete without message_ids, deletes that member's "
                         "lines from the last 48 h."))
        duration: Optional[str] = Field(
            default=None,
            description=("mute/ban length, e.g. 30m, 2h, 7d (max 30d). Default "
                         "mute 1h, ban 30d."))
        message_ids: Optional[List[str]] = Field(
            default=None,
            description="delete: the msg ids (from room_read's {msg=…}).")
        delete_messages: bool = Field(
            default=False,
            description=("ban/mute: also delete the target's lines from the "
                         "last 48 h (spam cleanup)."))
        reason: Optional[str] = Field(
            default=None, description="Why, for the owner's record.")

    @controller.registry.action(
        "Moderate a group room you are in (e.g. The Public Den): mute, unmute, "
        "ban or unban a member, or delete messages. Use room_read first to get "
        "the member's uid and the msg ids. The owner and room admins can never "
        "be targeted. The bot must be a room admin with the right permission. "
        "Act on clear rule-breaking (spam, scams, abuse) and tell the owner "
        "what you did.",
        param_model=RoomModerateAction,
    )
    async def room_moderate(params: RoomModerateAction,
                            execution_context=None) -> ActionResult:
        from core.security.owner_turn import owner_turn_refusal
        from agents.task.session_class import is_autonomous_session
        refusal = owner_turn_refusal(
            execution_context, verb="room_moderate", does="moderate a room",
            public="moderate a room")
        if refusal:
            return _out(refusal)
        verb = (params.action or "").strip().lower()
        if verb not in _VERBS:
            return _out(f"unknown action {params.action!r}; use one of "
                        f"{', '.join(_VERBS)}.")

        from tools.controller.room_read_action import _data_dir, room_matches
        home = _data_dir(controller)
        if not home:
            return _out("no data home resolved on this deploy.")
        container = getattr(controller, "container", None)
        moderator = None
        try:
            moderator = container.get_service("room_moderator") if container else None
        except Exception:
            moderator = None
        if moderator is None:
            return _out("no chat connection is running (the Telegram surface "
                        "is not up in this process), so I cannot moderate.")

        from core.surfaces import chat_policy as _chat_policy
        from core.surfaces.group_allowlist import GroupAllowlist
        from core.surfaces.room_label import room_name
        rooms = [r for r in GroupAllowlist(f"{home}/group_allowlist.db").list_all()
                 if r.get("status") == "active"]

        def _title(r):
            try:
                pol = _chat_policy.load_for_chat(home, r["surface"], r["chat_id"])
            except Exception:
                pol = None
            return room_name(r, pol)

        want = (params.room or "").strip()
        matches = [r for r in rooms if want and room_matches(r, want, _title(r))]
        if len(matches) != 1:
            listed = "; ".join(f"{_title(r)} ({r['surface']}:{r['chat_id']})"
                               for r in (matches or rooms)) or "none"
            why = ("matches more than one room" if matches
                   else "is not an allowlisted room")
            return _out(f"'{want}' {why}. Rooms: {listed}.")
        room = matches[0]
        surface, chat_id = room["surface"], str(room["chat_id"])
        if surface != "telegram":
            return _out(f"moderation is only wired for telegram, not {surface}.")

        authority = await _moderation_authority(
            controller, execution_context, chat_id, verb, params,
            autonomous=is_autonomous_session(getattr(execution_context, "session_id", None)),
            title=_title(room))
        if authority:
            return _out(authority)

        from core.surfaces.group_ledger import GroupLedger
        rows = GroupLedger(f"{home}/surfaces.db").tail(
            surface, chat_id, limit=_SCAN_ROWS)

        target_id, target_name = None, ""
        if params.target or verb != "delete":
            target_id, target_name = resolve_target(
                rows, params.target or "", allow_unseen=verb in ("unmute", "unban"))
            if target_id is None:
                return _out(target_name)

        from core.surfaces import room_actions
        try:
            held = set(await moderator.rights_async(surface, chat_id) or ())
        except Exception as e:
            logger.warning("room_moderate: rights probe failed (%s)", e)
            return _out(f"could not read my own permissions in this room ({e}); "
                        f"nothing was done.")

        if target_id is not None:
            protected, why = await room_actions._target_protection(
                container, surface, chat_id, str(target_id), "",
                eff=room_actions.effect(verb),
                target_status_fn=moderator.member_status_async)
            if protected:
                return _out(f"refused — {why[0]}")

        # 2. The ids to delete — resolved and CHECKED before anything is applied,
        # so a bad id can never return a refusal over a ban that already landed.
        wants_delete = verb == "delete" or (
            params.delete_messages and verb in ("mute", "ban"))
        ids: List[str] = []
        if wants_delete:
            ids = [str(m) for m in (params.message_ids or [])]
            if ids:
                # ⚠️ EVERY explicit id must name a line we can see from a sender
                # who passes the protections (and, with a target, from THAT
                # target) — so a delete can never reach the owner's or an
                # admin's post, whatever else was passed.
                by_id = {r.message_id: r for r in rows}
                checked = {}
                for mid in ids:
                    r = by_id.get(mid)
                    if r is None or r.sender_id in ("", "agent"):
                        return _out(f"msg {mid} is not a member line in this "
                                    f"room's ledger; nothing was done.")
                    if target_id is not None and r.sender_id != str(target_id):
                        return _out(f"msg {mid} was not sent by "
                                    f"{target_name or target_id}; nothing was done.")
                    if r.sender_id not in checked:
                        checked[r.sender_id] = await room_actions._target_protection(
                            container, surface, chat_id, str(r.sender_id), "",
                            target_status_fn=moderator.member_status_async)
                    protected, why = checked[r.sender_id]
                    if protected:
                        return _out(f"refused for msg {mid} — {why[0]}")
            elif target_id is not None:
                cutoff = time.time() - _DELETE_WINDOW_SEC
                ids = [r.message_id for r in rows
                       if r.sender_id == str(target_id) and r.ts > cutoff
                       and r.kind != "deleted"]
            if verb == "delete" and not ids:
                return _out("nothing to delete — pass message_ids or a target "
                            "with lines in the last 48 h.")
            if verb == "delete" and "can_delete_messages" not in held:
                return _out("I need the can_delete_messages permission in this "
                            "room (make the bot an admin with it).")

        lines: List[str] = []
        applied = False
        if verb != "delete":
            eff = room_actions.effect(verb)
            if eff.telegram_right not in held:
                return _out(f"I need the {eff.telegram_right} permission in "
                            f"this room (make the bot an admin with it).")
            seconds = 0
            if eff.needs_duration:
                spec = params.duration or _DEFAULT_DURATION[verb]
                seconds = room_actions.parse_duration(spec) or 0
                if not seconds:
                    return _out(f"{spec!r} is not a duration (use 30m, 2h, 7d).")
                seconds = min(seconds, eff.max_duration_sec)
            row = _Row(surface=surface, chat_id=chat_id, verb=eff.verb,
                       target_user_id=str(target_id), target_name=target_name,
                       duration_sec=seconds)
            res = await moderator(row, eff, int(time.time() + (seconds or 3600)))
            if not getattr(res, "ok", False):
                return _out(f"Telegram refused: {getattr(res, 'reason', '')}")
            applied = True
            dur = (f" for {room_actions.format_duration(seconds)}"
                   if seconds else "")
            lines.append(f"{target_name or target_id} (uid={target_id}) "
                         f"{verb}{'ned' if verb.endswith('ban') else 'd'}{dur}.")

        if wants_delete and ids:
            if "can_delete_messages" not in held:
                lines.append("could not delete messages: I need the "
                             "can_delete_messages permission in this room.")
            else:
                done, failed, gone = 0, [], []
                for mid in ids[:_MAX_DELETE]:
                    res = await moderator.delete_message_async(surface, chat_id, mid)
                    reason = str(getattr(res, "reason", "") or "")
                    if getattr(res, "ok", False):
                        done += 1
                        gone.append(mid)
                    else:
                        failed.append(f"{mid}: {reason}")
                        if "not found" in reason.lower():
                            gone.append(mid)   # already removed — still handled
                # 2026-10-06: mark them in the room log so the next run sees
                # "already handled", not a fresh offence to warn about again.
                try:
                    GroupLedger(f"{home}/surfaces.db").mark_deleted(surface, chat_id, gone)
                except Exception as e:
                    logger.warning("room_moderate: could not mark deleted rows (%s)", e)
                applied = applied or done > 0
                lines.append(f"deleted {done} of {min(len(ids), _MAX_DELETE)} "
                             f"message(s)." + (f" Failed: {'; '.join(failed[:5])}"
                                               if failed else ""))

        # The effect hook records this call as an external write (effect row in
        # core/verb_policy_rows.py). NOT `room_action_applied`: that kind is a
        # paid-offer MONEY event (core/activity_class.py).
        if applied:
            logger.info("room_moderate: %s %s:%s target=%s reason=%s", verb,
                        surface, chat_id, target_id, (params.reason or "")[:200])
        return _out(f"{_title(room)}: " + " ".join(lines)
                    + " Tell the owner what you did and why.")
