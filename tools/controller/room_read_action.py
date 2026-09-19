"""The `room_read` action — the agent READS an allowlisted room's ledger.

Owner rail 2026-09-15 19:41Z: "fix tg chat reading and deploy and restart".
Rob could POST to The Public Den but not read it: the t.me/s web preview is
off, the Telegram Bot API cannot read history at all, and no session action
covered the one store that already holds the lines — `group_ledger`
(`core/surfaces/group_ledger.py`, 044 §4.1), which the harness appends EVERY
allowlisted-room line to before any gate, precisely so "a line that triggers
nothing is still context". The room turn, the room-servicing goal
(`agents/task/goals/group_service.py`) and `/groups tail` read it; an ordinary
session could not. This is that read surface, over the SAME store.

⚠️ The source framing is honest by construction: this is the LOCAL room
ledger, not a live Telegram history fetch. Lines sent while the bot was not
in the room are absent; retention is bounded (14 days / 2000 rows per chat).
The action says so on every read.

⚠️ Read-only and it NEVER posts. Posting stays on `message` (or the bound
room session answering a mention) — one write rail, unchanged.

⚠️ Allowlisted rooms only: a requested room that is not on the owner's
group allowlist is refused WITH the list of real rooms — never a fake empty
read, which would read exactly like "the room said nothing".

⚠️ Correspondent-taint: the room ledger carries the owner's own lines; the
action name is enumerated in `correspondent_gate._HIGH_IMPACT_NAMES` so a
tainted session cannot read it.

⚠️ Registry-closure landmine: NO `from __future__ import annotations` in
this module — the registry introspects the closure's first-param annotation
to route the validated param model.

⚠️ Registered from `tools/controller/service.py`, NOT `action_registration.py`
(that file sits at its size-ratchet ceiling); same escape hatch
`avatar_action.py` established.
"""
import logging
import time
from typing import Optional

from pydantic import BaseModel, Field

from agents.task.agent.views import ActionResult

logger = logging.getLogger(__name__)

#: `GROUP_LEDGER_RETENTION_DAYS` default; stated in the source framing so the
#: agent never over-trusts the window. Kept in sync manually — it is prose,
#: not a policy the action enforces (the ledger prunes itself).
_RETENTION_NOTE = "retention is bounded (14 days / 2000 rows per chat)"

_MAX_LIMIT = 200


def _data_dir(controller) -> Optional[str]:
    from core.runtime_paths import container_data_home
    return container_data_home(getattr(controller, "container", None))


def _render_line(row) -> str:
    stamp = time.strftime("%m-%d %H:%MZ", time.gmtime(row.ts))
    flags = []
    if row.sender_is_bot:
        flags.append("bot")
    if row.kind == "edit":
        flags.append("edit")
    if row.media_path:
        flags.append("media")
    if row.mentions_bot:
        flags.append("mentions-you")
    flag = f" [{', '.join(flags)}]" if flags else ""
    text = (row.text or "").strip() or "(no text — media/caption row)"
    return f"[{stamp}] {row.sender_name or row.sender_id} ({row.role_at_write}){flag}: {text}"


def register_room_read_action(controller) -> None:
    """Register the read-only `room_read` action over the group ledger.

    Rides `GROUP_CHAT_ENABLED` — the same flag that makes rooms a concept.
    With rooms off there is nothing to read and no action is registered."""
    from core.surfaces.config import SurfaceConfig
    if not SurfaceConfig.group_chat_enabled():
        return

    class RoomReadAction(BaseModel):
        room: Optional[str] = Field(
            default=None,
            description=("Which allowlisted room to read: the chat id, "
                         "'surface:chat_id', or a distinctive fragment of its "
                         "title. Omit to list the rooms you are in with their "
                         "line counts."))
        limit: int = Field(
            default=50,
            description="How many of the most recent lines to return (1-200).")
        since_minutes: Optional[int] = Field(
            default=None,
            description=("Only lines newer than this many minutes. Omit for "
                         "the plain most-recent window."))

    @controller.registry.action(
        "Read what has been said in a group/channel room you are in (e.g. The "
        "Public Den): the most recent lines from the local room ledger, "
        "newest last. Read-only — to reply, use message(surface='telegram', "
        "target=<chat_id>) or answer a mention in the room. Omit `room` to "
        "list your rooms.",
        param_model=RoomReadAction,
    )
    async def room_read(params: RoomReadAction, execution_context=None) -> ActionResult:
        from core.surfaces.group_allowlist import GroupAllowlist

        home = _data_dir(controller)
        if not home:
            return ActionResult(
                extracted_content="room_read: no data home resolved on this "
                                  "deploy — cannot read the room ledger.",
                include_in_memory=True)

        rooms = [r for r in GroupAllowlist(
            f"{home}/group_allowlist.db").list_all() if r.get("status") == "active"]
        if not rooms:
            return ActionResult(
                extracted_content="room_read: no allowlisted rooms on this "
                                  "deploy (the owner adds one with "
                                  "`polyrob owner groups allow` or /groups "
                                  "allow here). Nothing to read.",
                include_in_memory=True)

        def _title(r):
            return r.get("note") or f"{r['surface']}:{r['chat_id']}"

        def _room_list(lines_seen=None):
            out = ["rooms you are in (owner-allowlisted):"]
            for r in rooms:
                seen = ""
                if lines_seen is not None:
                    seen = f" — {lines_seen.get((r['surface'], r['chat_id']), 0)} lines captured"
                out.append(f"• {r['surface']}:{r['chat_id']} \"{_title(r)}\"{seen}")
            return out

        want = (params.room or "").strip()
        if not want:
            from core.surfaces.group_ledger import GroupLedger
            ledger = GroupLedger(f"{home}/surfaces.db")
            counts = {}
            for r in rooms:
                counts[(r["surface"], r["chat_id"])] = len(
                    ledger.tail(r["surface"], r["chat_id"], limit=_MAX_LIMIT))
            body = "\n".join(_room_list(counts))
            return ActionResult(
                extracted_content=(
                    f"{body}\nsource: the local room ledger — every line the "
                    f"harness captured for each allowlisted room "
                    f"({_RETENTION_NOTE}); NOT a live Telegram history fetch. "
                    f"A room with 0 lines is not a broken room: Telegram never "
                    f"echoes a bot's OWN posts back, so a channel only you post "
                    f"to stays at 0 — the send receipt is the delivery "
                    f"confirmation. Read again with room=<chat_id or title "
                    f"fragment>."),
                include_in_memory=True)

        matches = [r for r in rooms
                   if want in (r["chat_id"], f"{r['surface']}:{r['chat_id']}")
                   or want.lower() in _title(r).lower()]
        if not matches:
            return ActionResult(
                extracted_content=(
                    f"room_read: '{want}' is not an allowlisted room — I will "
                    f"not read a room the owner has not put me in. "
                    + "\n".join(_room_list())),
                include_in_memory=True)
        if len(matches) > 1:
            listed = "\n".join(f"• {r['surface']}:{r['chat_id']} \"{_title(r)}\""
                               for r in matches)
            return ActionResult(
                extracted_content=(
                    f"room_read: '{want}' matches more than one room — "
                    f"disambiguate with the chat id:\n{listed}"),
                include_in_memory=True)

        room = matches[0]
        limit = max(1, min(int(params.limit or 50), _MAX_LIMIT))
        since_ts = (time.time() - int(params.since_minutes) * 60
                    if params.since_minutes else None)

        from core.surfaces.group_ledger import GroupLedger
        rows = GroupLedger(f"{home}/surfaces.db").tail(
            room["surface"], room["chat_id"], since_ts=since_ts, limit=limit)

        header = (f"room: {room['surface']}:{room['chat_id']} "
                  f"\"{_title(room)}\" — {len(rows)} line(s), newest last")
        source = (f"source: the local room ledger — every line the harness "
                  f"captured for this allowlisted room ({_RETENTION_NOTE}); "
                  f"NOT a live Telegram history fetch — lines sent while the "
                  f"bot was not in the room are absent.")
        if not rows:
            lines = ["(no lines captured in this window — the room has been "
                     "quiet, or the lines are older than the ledger retains. "
                     "NOTE: Telegram never delivers a bot's OWN posts back as "
                     "updates, so an empty ledger says nothing about whether "
                     "your posts rendered — the send receipt is the delivery "
                     "confirmation; lines appear here only when OTHERS write.)"]
        else:
            lines = [_render_line(r) for r in rows]
        footer = ("read-only: this action never posts. Reply via "
                  "message(surface='telegram', target=<chat_id>) or by "
                  "answering a mention in the room.")
        return ActionResult(
            extracted_content="\n".join([header, source, "", *lines, "", footer]),
            include_in_memory=True)
