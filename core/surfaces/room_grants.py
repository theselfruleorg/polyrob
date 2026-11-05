"""101: what the owner granted a room beyond its base toolset.

Three axes, and the first never moves (proposal 101 §3): the audience of a room
session is always PUBLIC; ``chat.room_tools`` adds tools to every turn in a room;
``chat.admin_tools`` names bundles an admin reaches through ``/admin <task>``, in a
SEPARATE session keyed to that room and that admin. Only the owner writes either;
both are read off a ``core/surfaces/chat_policy.py`` overlay.

Pure core and a LEAF: it imports only the capability table, so the room gate can
read it. The live reads are in ``core/surfaces/room_seat.py``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, FrozenSet, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Bundle:
    """A named set of controller actions and of whole tools."""
    actions: FrozenSet[str] = frozenset()
    tools: FrozenSet[str] = frozenset()


#: The closed bundle table. ⚠️ Adding a row is a security decision, and
#: ``tests/test_room_safety_ratchet.py`` keeps money, exec, recall and the rest out.
ADMIN_BUNDLES = {
    "moderation": Bundle(actions=frozenset({"room_read", "room_moderate"})),
    "files": Bundle(tools=frozenset({"filesystem"})),
    "http": Bundle(tools=frozenset({"http_request"})),
    "browser": Bundle(tools=frozenset({"browser"})),
}


#: Where `/admin` runs (U7). A grant anywhere else would claim what cannot run.
ADMIN_SURFACES = frozenset({"telegram"})
ADMIN_ELSEWHERE_TEXT = "/admin runs in a Telegram group: send /admin <task> there."


def live_admin_bundles(policy: Any) -> Tuple[str, ...]:
    """``chat.admin_tools`` that are real bundles: a hand-edited file never widens."""
    return tuple(b for b in (getattr(policy, "admin_tools", ()) or ()) if b in ADMIN_BUNDLES)


#: A capability that keeps a tool out of a member-wide grant, whatever its row
#: says. ⚠️ 101 R11: a pack writes its own row, so the row alone is not enough.
_NEVER_MEMBER_CAPS = frozenset({"money", "exec", "high_impact", "delegate_blocked",
                                "shared_identity"})
#: The room's own read tool: high-impact by table, untrusted-wrapped, and a
#: named exception at the room gate too.
_MEMBER_CAP_EXEMPT = frozenset({"web_fetch"})


def member_grantable() -> FrozenSet[str]:
    """Tool ids the owner may add to every turn in a room."""
    from core.tool_capabilities import TOOL_CAPABILITIES, row_field
    out = set()
    for tool, caps in TOOL_CAPABILITIES.items():
        if not row_field(tool, "room_grantable"):
            continue
        if tool not in _MEMBER_CAP_EXEMPT and (
                set(caps) & _NEVER_MEMBER_CAPS
                or any(str(c).startswith("writes_") for c in caps)):
            logger.warning("room grants: %s asks to be room-grantable and holds a "
                           "capability that refuses it", tool)
            continue
        out.add(tool)
    return frozenset(out)


def live_room_tools(policy: Any) -> Tuple[str, ...]:
    """``chat.room_tools`` that are grantable: a hand-edited file never widens."""
    allowed = member_grantable()
    return tuple(t for t in (getattr(policy, "room_tools", ()) or ()) if t in allowed)


def bundle_tools(names) -> Tuple[str, ...]:
    """The whole tools these bundles load."""
    out = []
    for name in names:
        for tool in sorted(ADMIN_BUNDLES[name].tools):
            if tool not in out:
                out.append(tool)
    return tuple(out)


# --- the gate's question (make_room_gate_hook) --------------------------------

def _bundle_of(action: str, tool: Optional[str]) -> Optional[str]:
    from core.tool_capabilities import CATALOG_ALIASES
    tool_id = CATALOG_ALIASES.get(tool, tool) if tool else tool
    for name, bundle in ADMIN_BUNDLES.items():
        if action in bundle.actions or (tool_id and tool_id in bundle.tools):
            return name
    return None


def is_bundle_call(action: str, tool: Optional[str]) -> bool:
    return _bundle_of(action, tool) is not None


def _param(params: Any, key: str) -> Any:
    if isinstance(params, dict):
        return params.get(key)
    return getattr(params, key, None)


#: Words in the admin's own line that ask for each ``room_moderate`` verb.
_VERB_WORDS = {"mute": ("mute", "silence"), "unmute": ("unmute",),
               "ban": ("ban", "kick", "remove"), "unban": ("unban",),
               "delete": ("delete", "remove", "clear")}

#: Said when a member's or a page's words may have led to this call.
TAINT_REFUSAL = ("this turn read outside text, so it cannot moderate. Tell the admin the "
                 "exact action and ask them to send /admin again, naming the person.")


def _names(line: str, value: str) -> bool:
    """Does the admin's own line name *value*, as a whole word?"""
    want = str(value or "").strip().lstrip("@")
    if not want:
        return False
    import re
    return re.search(rf"(?<![\w@-])@?{re.escape(want)}(?![\w-])", line, re.IGNORECASE) is not None


def _moderation_pins(seat: Any, action: str, params: Any) -> Optional[str]:
    surface, chat_id = seat.room()
    if str(_param(params, "room") or "").strip() not in (chat_id, f"{surface}:{chat_id}"):
        return f'name this room exactly: room="{surface}:{chat_id}"'
    if action != "room_moderate":
        return None
    target = str(_param(params, "target") or "").strip()
    ids = [str(m) for m in (_param(params, "message_ids") or [])]
    verb = str(_param(params, "action") or "").strip().lower()
    # 101 R17: the admin's line asks for the verb, or asking "is bob spamming?" names
    # the person and a planted line supplies the ban.
    wanted_verbs = [verb] + (["delete"] if _param(params, "delete_messages") else [])
    author = seat.reply_author
    if author:
        # 101 R4, R16: under a reply the target is required and it is the author's UID.
        # A name is self-chosen, so a member named like a victim would aim this at them.
        # The action checks every message id against the target. Only that author can
        # be reached, so outside text in this turn does not matter here.
        if target != str(author.get("uid") or ""):
            return ("this turn replies to one person, so it may moderate only that person: "
                    f"target=\"{author.get('uid')}\"")
        return _verb_refusal(seat.line, wanted_verbs)
    if seat.tainted:
        return TAINT_REFUSAL
    # 101 R5: the admin's CURRENT line names who, or a planted line in the history
    # could fire on any later `/admin ok`.
    if not target and not ids:
        return "name the member in your /admin line, or send it in reply to them"
    if target and not _names(seat.line, target):
        return f"{target} is not named in the admin's own line; ask them to name the person"
    missing = [m for m in ids if not _names(seat.line, m)]
    if missing:
        return "those message ids are not in the admin's own line; ask them to name them"
    return _verb_refusal(seat.line, wanted_verbs)


def _admin_named_url(line: str, params: Any) -> bool:
    url = str(_param(params, "url") or "").strip().rstrip("/")
    return bool(url) and url in line


def _verb_refusal(line: str, verbs) -> Optional[str]:
    for verb in verbs:
        if not any(_names(line, word) for word in _VERB_WORDS.get(verb, (verb,))):
            return (f"the admin's own line does not ask to {verb}; ask them to send "
                    f"/admin {verb} and the person")
    return None


def seat_decision(seat: Any, action: str, tool: Optional[str],
                  params: Any) -> Optional[Tuple[bool, str]]:
    """``None`` = not a grant question, the room rule answers. Else ``(allow, why)``.

    Asked for every call of a PUBLIC session. Nothing here widens a session that
    no stamp and no live grant names, and every fault refuses.
    """
    if seat is None:
        return None
    try:
        name = _bundle_of(action, tool)
        if name is None:
            if (seat.stamp and seat.tainted and "web_fetch" in (action, tool)
                    and not _admin_named_url(seat.line, params)):
                # 101: a planted line must not send the room log out through the one fetch
                # tool every room session holds. A page the admin's own line names is safe:
                # `/admin compare <a> and <b>` still reads both.
                return False, ("this turn read outside text, so it fetches only a page named "
                               "in the admin's own line. Ask them to send /admin with the page")
            return _member_grant_decision(seat, tool)
        stamp = seat.stamp
        if not stamp:
            if action in ADMIN_BUNDLES["moderation"].actions:
                return None            # the room rule refuses these by name
            # ⚠️ 101 R6: a session with no stamp is an ordinary room session, and
            # `read_file` is not denied by name. Only the toolset kept it out.
            return False, "this tool is not available in a room"
        if not seat.allowed():
            return False, "this room is no longer allowed"
        if seat.role() not in ("owner", "admin"):
            return False, "the person who asked no longer holds a room admin role here"
        policy = seat.policy()
        if getattr(policy, "unreadable", False):
            return False, "this room's settings could not be read"
        if name not in live_admin_bundles(policy):
            return False, "the owner has not granted that here"
        if name == "moderation":
            why = _moderation_pins(seat, action, params)
            return (False, why) if why else (True, "")
        if name == "http" and seat.tainted:
            # 101 R18: outside words must not choose what leaves this box.
            return False, ("this turn read outside text, so it sends no request. Tell the "
                           "admin the exact request and ask them to send /admin again")
        if name == "files" and not seat.files_ok():
            return False, "the workspace here is a shared folder, so files stay off"
        if name == "browser" and not seat.browser_ok():
            return False, ("this browser runs where it can reach the operator's logins or "
                           "private network, so it stays off in a room; it needs a remote "
                           "browser (BROWSER_CDP_URL or BROWSER_WSS_URL)")
        return True, ""
    except Exception as e:
        logger.warning("room grants: gate fault on %s (refusing): %s", action, e)
        return False, "the room's grants could not be read"


def _member_grant_decision(seat: Any, tool: Optional[str]) -> Optional[Tuple[bool, str]]:
    """A revoked ``chat.room_tools`` entry refuses at the next call."""
    if not tool:
        return None
    from core.tool_capabilities import row_field
    if not row_field(tool, "room_grantable"):
        return None
    if tool in seat.room_tools():
        return None
    return False, "the owner has removed that tool from this room"
