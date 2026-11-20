"""The chat seats' command catalogue: `/help`, the "/" menu, the unknown-verb answer,
the owner-verb set and the room refusals (proposal 125 S14).

Moved below the seats from ``surfaces/telegram/harness.py`` (which re-exports the old
private names): Telegram, the other chat surfaces on the shared executor and the
console chat box print the same help and refuse the same lines, and the console reaches
it without importing the Telegram package. Rendered from the ONE verb table
(``core.verbs``); this module owns only the argument grammar and the chat seats' own
elaborations. Core reads only.
"""
import difflib
from typing import Iterable

from core.verbs import owner_gated_names

#: The owner words for a seat id (``core.verbs.Verb.seats``).
_SEAT_WORDS = {"repl": "the terminal", "telegram": "Telegram", "console": "the console"}


def elsewhere_text(verb, seat: str) -> str:
    """The line for a real verb this seat cannot run, or "" when it can."""
    if verb is None or verb.runs_on(seat):
        return ""
    where = " or ".join(_SEAT_WORDS.get(s, s) for s in verb.run_seats())
    return (f"{verb.name} — {verb.help}.\nIt runs in {where}, not here. "
            f"Send /help for the verbs this chat has.")


def unknown_command_text(cmd: str, known: Iterable[str], seat: str) -> str:
    """The answer for a slash line no handler on *seat* owns (030 L9, 113 W0): a
    retired verb (what replaced it), a REAL verb of another seat (name the seat), or a
    typo (the closest verbs)."""
    from core.surfaces.verb_check import verb_note
    from core.verbs import verb_for
    if cmd == "/admin":
        from core.surfaces.room_grants import ADMIN_ELSEWHERE_TEXT
        return ADMIN_ELSEWHERE_TEXT
    if verb_note(cmd):
        return f"{cmd} is gone. {verb_note(cmd)} Send /help for the full list."
    elsewhere = elsewhere_text(verb_for(cmd), seat)
    if elsewhere:
        return elsewhere
    matches = difflib.get_close_matches(cmd, list(known), n=2, cutoff=0.6)
    hint = f" Did you mean {' or '.join(matches)}?" if matches else ""
    return f"Unknown command {cmd}.{hint} Send /help for the full list."


def agent_name() -> str:
    """The instance's own name for user-facing help — never a hardcoded bot name."""
    try:
        from core.instance import resolve_instance_id
        return resolve_instance_id()
    except Exception:
        return "the agent"


# ---------------------------------------------------------------------------
# /help — RENDERED FROM THE ONE VERB TABLE (D34)
#
# ⚠️ This used to be a hand-written wall of text: a SECOND grouping of the same
# verbs, with its own section names and its own sentences, beside
# ``core/verbs.py``. Nothing pinned the two together, so `/missed` said one
# thing here and another in the palette, `/start` had no line at all (and
# `/help start` therefore answered "unknown command"), and `/groups use`
# existed for weeks with no documentation anywhere.
#
# The table owns WHAT a verb is (``Verb.help``) and its SECTION (``Verb.group``,
# rendered in ``GROUP_ORDER``). This module owns only the two things the table
# does not carry: the ARGUMENT GRAMMAR (``USAGE``) and the chat seats' own
# elaborations (``DETAIL`` continuation lines and ``SUBVERBS``). A contract
# test (tests/unit/surfaces/telegram/test_help_is_verb_table.py) pins every
# command line's description against ``VERB_TABLE``.
# ---------------------------------------------------------------------------

#: verb -> the argument grammar rendered after the name. A verb absent here
#: takes no arguments.
USAGE = {
    "/pause": "[word…] [for 6h]",
    "/resume": "[word…]",
    "/missed": "[n]",
    "/run": "[pause|resume|stop <n>]",
    "/inbox": "[all|n]",
    "/more": "<token>",
    "/asks": "[<id>]",
    "/skills": "[words | <id>]",
    "/fulfill": "<id> [what you want to tell the run]",
    "/job": "[list|new|show|edit|pause|resume|run|stop] …",
    "/goal": "new <what> · <show|retry|pause|stop [--forever]|allow> <id>",
    "/apps": "[list|show|approve|reject|kill|start|remove|logs <slug>]",
    "/mcp": "[add <id> <url>|remove <id>|test <id>]",
    "/journey": "[window]",
    "/new": "[name]", "/chat": "<number or name>", "/rename": "<name>",
    "/close": "[number or name]",
    "/wallet": "[balances|tokens|signed [n]]",
    "/writeoff": "<chain> <address> [go] [reason]",
    "/unquarantine": "<chain> <address> [go]",
    "/invoices": "[status] | refunded <id> [tx]",
    "/settle": "<id> [tx]",
    "/trade": "start|status|pause|resume|stop|release", "/offer": "list|show|orders|quote|resolve|pause|resume|retire",
    "/bridge": "<from> <to> <amount> [from usdc] [as <asset>] [go]",
    "/lp": "positions|pool|quote|add|remove|collect|seed|move|launch",
    "/launch": "<SYMBOL> <name…> [logo <url>] [desc <text>] [buy <amount>] [go]",
    "/deploy": "<SYMBOL> <supply> <name…> [on <chain>] [decimals <n>] [go]",
    "/claim": "<token> [go]",
    "/pay": "<url> [max_usd] [go] [id=<name>]",
    "/send": "<amount> <native|token-address> to <address> on <chain> [max <usd>] [go]",
    "/swap": "<amount> <native|token> to <token> on <chain> [slippage <bps>] [max <usd>] [go]",
    "/cards": "[<id> [ok|no|re|1-6]]",
    "/nft": "[send <id> <to> [go]]|list|info <contract> <id>|account [<id>]|transfer <contract> <id> <to> [go]|"
            "revoke <contract> <operator> [go]|trust <collection>|untrust <collection>|ready <id>|unready <id>",
    "/dapp": "list|revoke <id>",
    "/contacts": "[<surface> <address>]",
    "/why": "[n]",
    "/thread": "[n | <hours>h]",
    "/identity": "register|set-uri [go]",
    "/allow": "<surface> <target>",
    "/deny": "<surface> <target>", "/pair": "[list|approve <surface> <code>|revoke <surface> <id>]",
    "/groups": "<allow|deny|list|use|mode|set|role|roles|tools|audit|tail|service|admins> "
               "here|<surface> <chat_id> [...]",
    "/mute": "here|<surface> <chat_id> <duration>",
    "/ban": "<duration> IN REPLY to a message",
    "/unmute": "IN REPLY to a message",
    "/unban": "IN REPLY to a message",
    "/paid": "<status|enable|disable|price <verb> <usd>|asset <id>|offers|"
             "cancel <id>>",
    "/prefs": "[all]",
    "/config": "[list | get|explain <key> | search <words> | set <key> <value> | check]",
    "/model": ("[<name> [default] [reasoning=<level>] | reset | aliases | heavy <name> | "
               "light <name> | choice free|listed]"),
    "/usage": "[days]",
    "/kb": "[list [collection] | search <query> | <words>]",
    "/files": "[n]",
    "/help": "[verb]",
}

#: verb -> extra INDENTED lines under its command line. They never start with
#: "/" so ``help_commands`` skips them, and ``help_for`` returns them with the
#: verb they belong to.
DETAIL = {
    "/pause": ("   everything, or: trading, background, messages, deploying "
               "— or a scope",),
    "/mute": ("   with a duration IN REPLY to a message it mutes that MEMBER "
              "— free for you and room admins, a paid action for a member",),
    "/unmute": ("   the muted member may COUNTER-PAY their own",),
    "/unban": ("   the banned member may COUNTER-PAY their own",),
    "/config": ("   safe keys write immediately; guarded keys queue for "
                "/pending review",),
    "/mcp": ("   https only; loads in every new session",),
    "/journey": ("   default 24h, e.g. 30m/24h/7d",),
    "/bridge": ("   quote only without `go`; your own `/bridge … go` passes the "
                "autonomous ceiling and the pause — the per-tx and daily caps "
                "still bind. A bridge I start above the ceiling asks you first",),
    "/launch": ("   quote only without `go`; a launch with no logo and no "
                "description looks abandoned",),
    "/deploy": ("   quote only without `go`; no mint function, no owner",),
    "/claim": ("   collects creator fees the launchpad escrow already owes me",),
    "/pay": ("   price only without `go`; pays in USDC on Base, never above max_usd",),
    "/identity": ("   mints THIS instance's ERC-8004 identity from my own "
                  "wallet; refuses if one already exists",),
}

#: verb -> extra full command lines for its SUBVERBS. A subverb is not a
#: ``_COMMANDS`` row, so it has no table entry and its words live here.
SUBVERBS = {
    "/wallet": ("/wallet autonomous <usd> — how much runs without asking you",
                "/wallet tokens — which tokens I trust, and what is quarantined",
                "/wallet trust|untrust <chain> <address> [go] — your word on one token",
                "/wallet income [<kinds> <percent>% to <token> on <network> [min <usd>] [go] "
                "| end [go]] — the share of income that buys a pinned token"),
}

#: The verb DELIBERATELY absent from the table, `/task` (``core.verbs.
#: PALETTE_EXCLUDED`` — the console drops it from /help). The chat seats still
#: document it, so it is rendered here with its own words, in its section.
LOCAL_ROWS = {
    "talk": (
        ("/task", "<goal>", "The old form of /new: a separate chat that starts with your goal"),
    ),
}


def help_line(name: str, usage: str, text: str) -> str:
    return f"{name}{(' ' + usage) if usage else ''} — {text}"


#: The seat id the chat catalogue renders for (``core.verbs.Verb.seats``): the chat
#: apps on the shared executor run Telegram's verb set; the console filters it.
SEAT = "telegram"


def runs_here(verb, seat: str = SEAT) -> bool:
    """True when *seat* can run *verb*: the verb table's own rule
    (:meth:`core.verbs.Verb.runs_on`), and a room verb only where a room transport
    is registered (``core.surfaces.room_transport.seat_has_rooms`` — the registry
    answers, never a list of surface names)."""
    if not verb.runs_on(seat):
        return False
    from core.surfaces.room_transport import ROOM_VERBS, seat_has_rooms
    return verb.name not in ROOM_VERBS or seat_has_rooms(seat)


def seat_groups(seat: str = SEAT) -> list:
    """``[(group, (Verb, …))]`` that *seat* can run, in ``core.verbs.GROUP_ORDER``."""
    from core.verbs import grouped
    out = []
    for group, verbs in grouped(seat):
        rows = tuple(v for v in verbs if runs_here(v, seat))
        if rows:
            out.append((group, rows))
    return out


def build_help_body(seat: str = SEAT) -> str:
    """The `/help` body, rendered from :data:`core.verbs.VERB_TABLE`.

    ⚠️ Filtered to the verbs THIS seat can run (:func:`runs_here`). A REPL-local
    verb (`/gates`, `/meter`) never reaches a Telegram message at all, and a room
    verb fails on a chat app with no room transport, so listing either would
    promise what the seat cannot do.
    """
    from core.verbs import GROUP_ORDER, empty_group_hints
    body = dict(seat_groups(seat))
    hints = dict(empty_group_hints(seat))    # 067 P5a: "install the wallet pack"
    lines = []
    for group in GROUP_ORDER:
        verbs = body.get(group, ())
        rows = [f"  {hints[group]}"] if group in hints else []
        for verb in verbs:
            rows.append(help_line(verb.name, USAGE.get(verb.name, ""),
                                   verb.help))
            rows.extend(DETAIL.get(verb.name, ()))
            rows.extend(SUBVERBS.get(verb.name, ()))
        for name, usage, text in LOCAL_ROWS.get(group, ()):
            rows.append(help_line(name, usage, text))
            rows.extend(DETAIL.get(name, ()))
        if not rows:
            continue
        lines.append(f"— {group[:1].upper()}{group[1:]} —")
        lines.extend(rows)
    lines.append("Or just send a message to talk to {name}.")
    return "\n".join(lines)


_BODY: dict = {}


def help_body(seat: str = SEAT) -> str:
    """SSOT for /help AND the setMyCommands menu (help_commands() parses the "/"
    lines; section headers and indented detail lines are skipped). Built once per
    seat (and per room-transport answer, which a surface registers at load), on
    first use — the Telegram harness builds it at import, as it always did."""
    from core.surfaces.room_transport import seat_has_rooms
    key = (seat or SEAT, seat_has_rooms(seat or SEAT))
    if key not in _BODY:
        _BODY[key] = build_help_body(key[0])
    return _BODY[key]

#: G11: the slash list is not the whole control surface — the agent can act
#: through its own tools when asked in prose ("schedule a check every morning at
#: 9", "add a goal to …"). That path predates the slash verbs and is strictly
#: wider than them, but nothing ever told the owner it existed, so /help read as
#: an exhaustive list of what was possible.
HELP_PROSE_NOTE = (
    "\n\nYou don't have to use a command. Ask me in plain words — "
    "\"schedule a check every morning at 9\", \"add a goal to …\", "
    "\"what did you spend this week\" — and I'll use my own tools."
)


def help_text(seat: str = SEAT) -> str:
    name = agent_name()
    return f"{name} commands:\n" + help_body(seat).format(name=name) + HELP_PROSE_NOTE


def help_for(verb: str, seat: str = SEAT) -> str:
    """`/help <verb>` (030 WS-C4): the verb's line(s) from the SSOT — its
    command line, its indented detail, and its subverb lines like
    `/wallet tokens` — without triggering the verb.

    ⚠️ D42: an ALIAS resolves through the table too, so `/help h` answers for
    `/help`. And `/start` now has a row, so `/help start` is no longer the
    "unknown command" reply for a verb the bot itself sends on first contact.

    ⚠️ A SEAT-LOCAL verb (`/gates`, `/meter` — `core.verbs.Verb.seats`) is
    deliberately absent from this seat's body, so it found no line and fell
    through to "Unknown command". That is a confident lie about a verb that
    exists: the honest answer names the seat it runs on. Asking about
    something real must never be answered as if it were a typo.
    """
    from core.verbs import verb_for
    raw = "/" + verb.strip().lstrip("/").lower()
    canonical = verb_for(raw)
    v = canonical.name if canonical is not None else raw
    if canonical is not None and not canonical.runs_on(seat):
        return unknown_text(v)
    if canonical is not None and not runs_here(canonical, seat):
        from core.surfaces.room_transport import registered_surfaces
        apps = ", ".join(registered_surfaces())
        return (f"{v} acts in a group room, and this chat app has no room connection. "
                + (f"Use it on {apps}." if apps else "No chat app with rooms is connected."))
    out: list = []
    collecting = False
    for ln in help_body(seat).splitlines():
        if ln.startswith("/"):
            collecting = (ln.split(" ")[0] == v)
            if collecting:
                out.append(ln)
            continue
        if collecting and ln.startswith(" "):
            out.append(ln)
            continue
        collecting = False
    if not out:
        return unknown_text(v)
    return "\n".join(out)


def welcome_text(owner: bool = True) -> str:
    """First-contact reply for /start (030 L9) — short, not the verb catalog. Only
    the owner is pointed at the owner's verbs; anyone else at the help they can use."""
    name = agent_name()
    return (
        f"👋 I'm {name}. Tell me in plain words what you need, for example:\n"
        "• \"check my wallet every morning at 9\"\n"
        "• \"read this link and give me the three main points\"\n"
        "• \"what did you spend this week?\"\n\n"
        + ("What needs you: /inbox · how I am: /status · every command: /help" if owner
           else "The commands you can use here: /help")
    )


def unknown_text(cmd: str) -> str:
    """A slash line no handler here owns: a retired verb, a verb of another seat, or
    a typo with its closest verbs (:func:`unknown_command_text`)."""
    return unknown_command_text(cmd, [f"/{name}" for name, _ in help_commands()], SEAT)


#: proposal 125 S11: DERIVED from the one verb table — every routed row without
#: ``any_principal``, so a new row is owner-gated by default (fail closed).
OWNER_ADMIN_COMMANDS = tuple(sorted(owner_gated_names()))

#: 044 I10 (spec §6.1 P9/P10): owner verbs that may NOT execute from inside a
#: room, whoever is watching. Money (`/trade /wallet /bridge /deploy /launch
#: /invoices /settle`), autonomy control
#: (`/pause /halt /resume /approve /reject`), and access/configuration (`/allow
#: /deny /config /prefs /mcp /apps`). Redirecting the REPLY to the owner's DM was
#: never enough: the ACTION ran, so a member who talked the owner into typing one
#: got it, and the room is the one place a shoulder-surfer is guaranteed.
#:
#: What STAYS reachable from a room — read-only or room-scoped, and answered in
#: the owner's DM: `/status /help /groups /mute /paid /cancel /new /goals
#: /recap /journey /missed`. A verb in NEITHER list is unchanged (it was never
#: a room hazard; adding one here is a deliberate act).
#:
#: ⚠️ `/paid` (046) is deliberately ABSENT: it configures THIS room and nothing
#: else, its writes are the room's own overlay, and it names no money the agent
#: can move. Refusing it from the room would send an admin to a DM to configure
#: the room he is standing in.
#: D43: `/job`, `/goal` and `/fulfill` were MISSING. Each of them MUTATES
#: durable autonomous work — scheduling a recurring run, cancelling a goal,
#: declaring an owner ask satisfied — from inside a room where a member can
#: read the ids off the screen and talk the owner into typing one. The
#: ratchet (tests/unit/surfaces/telegram/test_room_refusal_ratchet.py) now
#: derives the requirement instead of trusting this list to be remembered.
#:
#: `/contacts` is here too: a transcript with a third party is private
#: correspondence, and the room is the one place a shoulder-surfer is
#: guaranteed. The new money verbs (`/claim`, `/nft`, `/dapp`, `/identity`)
#: join `/trade` and the rest for the same reason they did.
#: W4: DERIVED — every routed verb whose ``core.verbs`` row is room-refused (a
#: core row's ``room_refused`` flag, or a contributed verb its contributor did not
#: name room-safe). The per-verb reasons live beside the rows.
def build_room_refused() -> frozenset:
    from core.surfaces.dispatcher import _COMMANDS
    from core.verbs import room_refused
    return frozenset(n for n in _COMMANDS if room_refused(n))




#: TG7 (audit 2026-10-03): a room-reachable READ verb whose SUBVERBS write.
#: `/avatar` shows the face from a room; `/avatar set|clear|push` changes it, and
#: like every other owner write never runs from a room.
ROOM_REFUSED_SUBVERBS = {
    "/avatar": frozenset({"set", "clear", "push"}),
    "/book": frozenset({"retire"}),  # 086 W0: the book read stays; a retire writes
}


def room_refused_line(cmd: str, text: str) -> bool:
    """The room refusal for one LINE: the verb, or a refused subverb of it."""
    if room_refused(cmd):
        return True
    words = (text or "").split()
    sub = words[1].lower() if len(words) > 1 else ""
    return sub in ROOM_REFUSED_SUBVERBS.get(cmd, frozenset())


def owner_verbs() -> frozenset:
    """The owner verbs this seat runs: its own tuple plus every verb a pack
    contributed (``core.verbs.register_verbs``, 067 P5a)."""
    from core.verbs import routed_names
    return frozenset(OWNER_ADMIN_COMMANDS) | routed_names()


def room_refused(cmd: str) -> bool:
    """044 I10: the ``core.verbs`` row decides (a pack verb registered after
    import included)."""
    from core.verbs import room_refused
    return room_refused(cmd)


def help_commands() -> list:
    """``(command, description)`` pairs for Telegram's ``setMyCommands`` "/" menu.

    064 F2: rendered by the ONE pure menu function, ``core.verbs.menu_entries``,
    from the same table and local rows as ``/help`` — so the menu can never drift
    from it, and a duplicate name (which makes ``setMyCommands`` reject the WHOLE
    menu) is dropped there, first occurrence kept.
    """
    from core.verbs import menu_entries
    return menu_entries(SEAT, LOCAL_ROWS)
