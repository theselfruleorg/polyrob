"""h_rooms.py — the room + lifecycle verbs the REPL was missing (C18 / E4).

``/groups /mute /unmute /ban /unban /paid`` and ``/cancel /new /start``. Nine
verbs ``core.verbs`` already describes for every seat, and which the terminal —
the seat an owner uses when the phone is not the answer — simply did not have.

⚠️ REACH, never policy. The room verbs are THIN calls into
``surfaces.telegram.group_ops``' reply helpers, the same way ``h_money_verbs``
calls ``owner_ops``: every role gate, the default-DENY room allowlist, the 031
pause and the paid-rail caps stay exactly where they are and decide exactly
what they decided before. Nothing here grants an authority.

⚠️ The helpers were written against a Telegram ROUTING RESULT, not a string —
they read the sender's identity, the room the line arrived in, and (for the
paid rail) the message it replied to. This module builds the one honest
stand-in a terminal can offer: :class:`_CliResult`, whose "room" is whatever
the owner NAMED (``--chat <id>``, or the explicit ``<surface> <chat_id>`` pair
the helpers already parse) and whose "reply target" is always absent, because a
terminal cannot reply to a Telegram message. So ``/mute`` reaches its ROOM
meaning here and its member meaning refuses with the helper's own sentence —
which is true, and better than a guess.

⚠️ The caller's room role is ``owner`` only when ``core.instance.is_owner(uid,
local=True)`` says so — the documented owner check for the ``{cli,local,repl}``
surfaces, the same one ``/pending`` and ``/reject`` already use. It is not a
new grant; a non-owner REPL falls back to ``member`` and the helpers refuse.
"""
from __future__ import annotations

from typing import Any, List, Optional, Tuple

from cli.ui import candy

#: The surface a bare ``--chat <id>`` means. Rooms exist on Telegram today; an
#: explicit ``--chat <surface>:<id>`` names any other.
_DEFAULT_ROOM_SURFACE = "telegram"

_NO_ROOM = (
    "this verb acts on a chat room; run it in that room, or name the room "
    "here — `--chat <chat_id>` (or `--chat <surface>:<chat_id>`), or pass the "
    "`<surface> <chat_id>` pair the verb already takes. `/groups list` shows "
    "the rooms."
)


# ---------------------------------------------------------------------------
# The terminal's stand-in for a Telegram routing result
# ---------------------------------------------------------------------------


class _CliSource:
    """The ``identity.source`` the helpers read: which room, of which kind."""

    def __init__(self, surface: Optional[str], chat_id: Optional[str]) -> None:
        self.surface_id = surface
        self.chat_id = chat_id
        # No named room => this seat behaves as a DM (the helpers' own word for
        # "you are not standing in the room"), which is exactly true.
        self.chat_type = "supergroup" if (surface and chat_id) else "dm"


class _CliIdentity:
    def __init__(self, user_id: str, role: Optional[str],
                 surface: Optional[str], chat_id: Optional[str]) -> None:
        self.user_id = user_id
        self.raw_user_id = user_id
        self.source = _CliSource(surface, chat_id)
        #: Only ever set for the room this line NAMES — `_targets_here` checks
        #: that, so the stamp can never carry across to another room.
        self.chat_role = role


class _CliInbound:
    def __init__(self, identity: _CliIdentity) -> None:
        self.identity = identity
        #: No raw update: a terminal line replies to no message, so
        #: ``_reply_target`` correctly answers "no member named here".
        self.raw = None
        self.text = ""


class _CliResult:
    def __init__(self, inbound: _CliInbound) -> None:
        self.inbound = inbound


def _tenant(ctx) -> str:
    from cli.ui.commands.h_owner import _tenant as _t
    return _t(ctx)


def _role(ctx) -> Optional[str]:
    """``"owner"`` for the local operator, else ``None`` (resolve normally)."""
    try:
        import core.instance as _ci
        return "owner" if _ci.is_owner(_tenant(ctx), local=True) else None
    except Exception:
        return None


def _split_chat_flag(args: List[str]) -> Tuple[List[str], Optional[str], Optional[str]]:
    """Pull ``--chat <surface>:<id>`` / ``--chat <id>`` out of *args*."""
    out: List[str] = []
    surface = chat_id = None
    i = 0
    while i < len(args):
        token = str(args[i])
        value = None
        if token == "--chat" and i + 1 < len(args):
            value = str(args[i + 1])
            i += 2
        elif token.startswith("--chat="):
            value = token.split("=", 1)[1]
            i += 1
        else:
            out.append(token)
            i += 1
            continue
        if ":" in value:
            surface, chat_id = value.split(":", 1)
        else:
            surface, chat_id = _DEFAULT_ROOM_SURFACE, value
    return out, surface, chat_id


def _named_room(args: List[str]) -> Tuple[Optional[str], Optional[str]]:
    """The room an explicit ``<verb> <surface> <chat_id> …`` line names.

    Mirrors ``group_ops._split_target``'s own rule (``len(rest) >= 2``) so the
    stand-in's "here" is the SAME room the helper will resolve — otherwise the
    role stamp would be checked against one room and the write applied to
    another.
    """
    rest = args[1:] if args else []
    if len(rest) >= 2 and rest[0].lower() != "here":
        return rest[0], rest[1]
    return None, None


def _result(ctx, args: List[str]) -> Tuple[Any, List[str], bool]:
    """``(result, args, has_room)`` — the stand-in plus the cleaned args."""
    args, surface, chat_id = _split_chat_flag(list(args or []))
    if not surface:
        surface, chat_id = _named_room(args)
    elif args and args[0].lower() not in ("list", "use"):
        # `--chat` supplies the target, so hand the helper its own `here` token
        # unless the line already names a pair.
        if not _named_room(args)[0]:
            args = [args[0], "here"] + args[1:]
    identity = _CliIdentity(_tenant(ctx), _role(ctx), surface, chat_id)
    return _CliResult(_CliInbound(identity)), args, bool(surface and chat_id)


def _emit_reply(ctx, value: Any, *, title: str) -> None:
    """Render a helper's answer — ``str`` or ``CommandReply`` — as text.

    A ``CommandReply`` marked ``to_room`` is still shown here: from the
    terminal there is no room to send it to, and swallowing it would be the
    silence this whole audit is about. The destination is NAMED instead.
    """
    from core.surfaces.command_reply import CommandReply
    if isinstance(value, CommandReply):
        text = value.text
        if value.to_room:
            text = f"{text}\n{candy.GUTTER}(on a chat surface this answer goes to the room)"
        ctx.emit(text, title=title)
        return
    ctx.emit(str(value), title=title)


async def _room_verb(ctx, helper, *, title: str, needs_room: bool) -> None:
    args = list(getattr(ctx, "args", None) or [])
    result, args, has_room = _result(ctx, args)
    if needs_room and not has_room:
        ctx.emit(f"{candy.GUTTER}{_NO_ROOM}", title=title)
        return
    try:
        _emit_reply(ctx, await helper(ctx.task_agent, result, args), title=title)
    except Exception as exc:  # fail-open: a room store is never a REPL teardown
        ctx.emit(f"{candy.GUTTER}({title} unavailable: {exc})", title=title)


async def h_groups(ctx) -> None:
    """Room presence admin: allow, deny, list, use, mode, set, role, tail…"""
    from surfaces.telegram.group_ops import groups_reply
    # `list` needs no room; every other verb resolves its own target and says
    # so when it has none — so this verb never pre-refuses.
    await _room_verb(ctx, groups_reply, title="groups", needs_room=False)


async def h_mute(ctx) -> None:
    """Silence the agent in a room (a member mute needs the room itself)."""
    from surfaces.telegram.group_ops import mute_reply
    await _room_verb(ctx, mute_reply, title="mute", needs_room=False)


async def h_unmute(ctx) -> None:
    """End a member's mute early — names a person, so it needs the room."""
    from surfaces.telegram.group_ops import unmute_reply
    await _room_verb(ctx, unmute_reply, title="unmute", needs_room=False)


async def h_ban(ctx) -> None:
    """Ban a member for a while — names a person, so it needs the room."""
    from surfaces.telegram.group_ops import ban_reply
    await _room_verb(ctx, ban_reply, title="ban", needs_room=False)


async def h_unban(ctx) -> None:
    """Lift a member's ban — names a person, so it needs the room."""
    from surfaces.telegram.group_ops import unban_reply
    await _room_verb(ctx, unban_reply, title="unban", needs_room=False)


async def h_paid(ctx) -> None:
    """A room's paid actions: status, prices, enable/disable, offers, cancel."""
    from surfaces.telegram.group_ops import paid_reply
    await _room_verb(ctx, paid_reply, title="paid", needs_room=True)


# ---------------------------------------------------------------------------
# Lifecycle: /cancel, /new, /start
# ---------------------------------------------------------------------------

#: The live ``persistent_loop.TurnController``, published by
#: ``cli/commands/chat.py`` when it builds one. A holder rather than an import
#: because the controller is created per REPL run, and ``CommandContext`` is
#: rebuilt per line.
_TURN: dict = {}


def set_turn_controller(controller: Any) -> None:
    """Publish the REPL's turn controller so ``/cancel`` can reach it."""
    _TURN["ctrl"] = controller


def current_turn_controller() -> Any:
    return _TURN.get("ctrl")


def h_cancel(ctx) -> None:
    """Stop the task running right now.

    ⚠️ ``/cancel`` must be reachable WHILE a turn runs, so it is in
    ``cli.ui.input_policy.LIVE_COMMANDS`` — otherwise the input the owner typed
    to stop the turn would be rejected by the turn he is trying to stop.
    """
    ctrl = current_turn_controller()
    if ctrl is None:
        ctx.emit(f"{candy.GUTTER}this REPL has no interruptible turn loop "
                 f"(press Ctrl-C to interrupt).", title="cancel")
        return
    if not getattr(ctrl, "busy", False):
        ctx.emit(f"{candy.GUTTER}nothing is running.", title="cancel")
        return
    try:
        ctrl.interrupt()
    except Exception as exc:
        ctx.emit(f"{candy.GUTTER}(could not cancel: {exc})", title="cancel")
        return
    ctx.emit("Task cancelled.", title="cancel")


def h_new(ctx) -> None:
    """Start fresh: cancel anything running, then clear this conversation.

    ⚠️ Honest scope. On a chat surface ``/new`` drops the chat→session binding
    so the next message routes cold. A REPL has ONE session for the life of the
    process, so this clears the conversation and says so rather than claiming a
    new session id it did not create.
    """
    from cli.ui.commands.handlers import _h_clear

    ctrl = current_turn_controller()
    if ctrl is not None and getattr(ctrl, "busy", False):
        try:
            ctrl.interrupt()
        except Exception:
            pass
    _h_clear(ctx)
    ctx.emit(f"{candy.GUTTER}Started fresh — history cleared. The session id is "
             f"still {ctx.session_id or '—'}; leave and re-run `polyrob` for a "
             f"new one.", title="new")


def h_start(ctx) -> None:
    """A welcome and a short tour of what I can do."""
    from cli.ui.first_run_notice import _resolve_context, build_first_run_notice
    try:
        autonomy_on, data_dir, config_path = _resolve_context()
        lines = build_first_run_notice(autonomy_on=autonomy_on, data_dir=data_dir,
                                       config_path=config_path)
    except Exception as exc:
        ctx.emit(f"{candy.GUTTER}(welcome unavailable: {exc})", title="start")
        return
    lines = list(lines) + ["", "  /help lists every command. /inbox is what is "
                           "waiting on you."]
    ctx.emit("\n".join(lines), title="start")


HELP_GROUPS = (
    "  Which rooms I am present in and how I behave there: allow or deny a\n"
    "  room, set its mode, grant a role, read its tail.\n"
    "\n"
    "  A room verb acts on a ROOM, and this terminal is in none — name the\n"
    "  room with --chat <chat_id>, or give the <surface> <chat_id> pair the\n"
    "  verb already takes. /groups list needs neither.\n"
    "\n"
    "  Rooms are default-DENY: a chat does nothing until it is allowed.",
    "`/groups` on Telegram (inside the room, `here` names it), and "
    "`polyrob owner groups`.",
)

HELP_MUTE = (
    "  Two meanings, separated by a fact, never a guess. Naming a ROOM\n"
    "  silences ME there for a while. Replying to a person's message mutes\n"
    "  THAT person — and a terminal cannot reply to a chat message, so the\n"
    "  member form says so instead of guessing who you meant.\n"
    "\n"
    "    /mute --chat <chat_id> 2h      silence me in that room for 2h\n"
    "    /mute telegram <chat_id> 2h    the same, spelled out",
    "`/mute` on Telegram — in the room, in reply to a member, or by name.",
)

HELP_PAID = (
    "  What a room SELLS: its price list, its caps, its open offers. Reading\n"
    "  and withdrawing an offer is reachable by that room's admin too;\n"
    "  changing what is sold is yours alone.\n"
    "\n"
    "  Needs a room: --chat <chat_id> names one from here.",
    "`/paid` on Telegram, inside the room or pointed at one.",
)

HELP_CANCEL = (
    "  Stop the task I am running right now. The turn ends where it is; the\n"
    "  conversation and everything I have already written stay.\n"
    "\n"
    "  Ctrl-C does the same thing.",
    "`/cancel` on Telegram.",
)

HELP_NEW = (
    "  Start fresh: anything running stops and this conversation's history is\n"
    "  cleared, keeping my instructions.\n"
    "\n"
    "  The session ID does not change here — one terminal run is one session.\n"
    "  Leave and start me again for a genuinely new one.",
    "`/new` on Telegram, where it also drops the chat's session binding.",
)

HELP_START = (
    "  A welcome: whether I act on my own, where my data and config live, and\n"
    "  which tools are on. The same notice a first run prints.",
    "`/start` on Telegram.",
)


def register(reg, Command) -> None:
    """Register the six room verbs + the three lifecycle verbs."""
    reg.register(Command(
        "groups", h_groups,
        "Room presence admin: allow, deny, mode, roles, and more",
        usage="list | <verb> [--chat <id>|<surface> <chat_id>] …",
        group="set up", help_long=HELP_GROUPS[0], elsewhere=HELP_GROUPS[1],
    ))
    reg.register(Command(
        "mute", h_mute, "Silence a room, or mute a member for a while",
        usage="[--chat <id>|<surface> <chat_id>] <duration>", group="set up",
        help_long=HELP_MUTE[0], elsewhere=HELP_MUTE[1],
    ))
    reg.register(Command(
        "unmute", h_unmute, "End a member's mute early",
        usage="[--chat <id>] …", group="set up",
    ))
    reg.register(Command(
        "ban", h_ban, "Ban a member for a while",
        usage="[--chat <id>] …", group="set up",
    ))
    reg.register(Command(
        "unban", h_unban, "Lift a member's ban",
        usage="[--chat <id>] …", group="set up",
    ))
    reg.register(Command(
        "paid", h_paid, "Paid room actions: status, pricing, and offers",
        usage="--chat <id> [status|prices|offers|enable|disable|price|cancel]",
        group="money", help_long=HELP_PAID[0], elsewhere=HELP_PAID[1],
    ))
    reg.register(Command(
        "cancel", h_cancel, "Stop the task I am running now", group="control",
        help_long=HELP_CANCEL[0], elsewhere=HELP_CANCEL[1],
    ))
    reg.register(Command(
        "new", h_new, "Start fresh: stop the turn and clear this conversation",
        group="talk", help_long=HELP_NEW[0], elsewhere=HELP_NEW[1],
    ))
    reg.register(Command(
        "start", h_start, "A welcome and a short tour of what I can do",
        group="talk", help_long=HELP_START[0], elsewhere=HELP_START[1],
    ))
