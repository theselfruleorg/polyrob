"""The short `/help` menu (2026-11-20 Telegram UX review, H1/H2).

`/help` was the whole verb catalog — 8.5k characters in three messages, the full
``a|b|c`` grammar of every verb. Now:

* ``/help``           the daily verbs, then one button per group;
* ``/help <group>``   one short line per verb of that group (``core.verbs.grouped``);
* ``/help <command>`` the verb's full lines (the harness's ``_help_for``);
* ``/help all``       the whole catalog, as before.

Every view renders from :data:`core.verbs.VERB_TABLE`; a button is the typed line
(``core.surfaces.nav``), so the text names each command for a seat with no buttons.
"""
from __future__ import annotations

from typing import Any, List, Optional, Tuple

#: The default seat id in ``core.verbs.Verb.seats``; every view takes the asking
#: seat (the chat apps share this harness), so a verb that seat cannot run is not
#: listed (``core.surfaces.command_catalog.runs_here``).
SEAT = "telegram"
#: How many of the daily verbs (``core.verbs.MENU_FIRST``, the ONE list the "/"
#: menu also leads with) the menu names first.
_MOST_USED_N = 6
#: Verb names shown on a group's line in the menu.
_PREVIEW = 4


def groups(seat: str = SEAT) -> List[Tuple[str, list]]:
    """``[(group, [Verb, …])]`` *seat* shows, in ``core.verbs.GROUP_ORDER``."""
    from core.surfaces.command_catalog import seat_groups
    return [(g, list(verbs)) for g, verbs in seat_groups(seat or SEAT)]


def most_used(seat: str = SEAT) -> List[str]:
    """The first daily verbs of ``core.verbs.MENU_FIRST`` this seat runs."""
    from core.verbs import MENU_FIRST, verb_for
    from core.surfaces.command_catalog import runs_here
    out = [n for n in MENU_FIRST if (v := verb_for(n)) is not None and runs_here(v, seat)]
    return out[:_MOST_USED_N]


def start_reply(user_id: Optional[str], owner: bool) -> Any:
    """`/start`: the welcome; the owner also gets buttons for the daily reads. Anyone
    else gets no button for a verb only the owner may run."""
    from core.surfaces.command_catalog import welcome_text
    from core.surfaces.command_reply import CommandReply
    from core.surfaces.nav import NavButton, actions
    buttons = ([NavButton("What needs me", "/inbox"), NavButton("Status", "/status"),
                NavButton("My goals", "/goals"), NavButton("Help", "/help")] if owner
               else [NavButton("Help", "/help")])
    return CommandReply(text=welcome_text(owner),
                        actions=tuple(actions(user_id, buttons)))


def title(group: str) -> str:
    return group[:1].upper() + group[1:]


def menu_reply(user_id: Optional[str], name: str, seat: str = SEAT) -> Any:
    from core.surfaces.command_reply import CommandReply
    from core.surfaces.nav import NavButton, actions
    rows = groups(seat)
    lines = [f"**{name} — what do you want to do?**",
             "Most used: " + " · ".join(most_used(seat)), "",
             "Groups (tap one, or send /help <group>):"]
    for g, verbs in rows:
        names = ", ".join(v.name for v in verbs[:_PREVIEW])
        lines.append(f"• {title(g)} — {names}" + (" …" if len(verbs) > _PREVIEW else ""))
    lines += ["", "Every command: /help all · one command: /help <command>",
              "Or just tell me in plain words."]
    buttons = [NavButton(title(g), f"/help {g}") for g, _v in rows]
    buttons.append(NavButton("All commands", "/help all"))
    return CommandReply(text="\n".join(lines), actions=tuple(actions(user_id, buttons)))


def group_reply(group: str, user_id: Optional[str], seat: str = SEAT) -> Any:
    from core.surfaces.command_reply import CommandReply
    from core.surfaces.nav import NavButton, actions
    verbs = dict(groups(seat)).get(group, [])
    lines = [f"**{title(group)}**"] + [f"{v.name} — {v.help}" for v in verbs]
    lines += ["", "How to use one: /help <command>"]
    return CommandReply(text="\n".join(lines),
                        actions=tuple(actions(user_id, [NavButton("◀ Menu", "/help")])))
