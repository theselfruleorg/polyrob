"""The owner's standing-work verbs, contributed to ``core.verbs``.

``/adopt`` (2026-10-08): the owner makes an AGENT-authored cron job or goal his
own after seeing its rig, target, tools and write verb; the confirm is an
action card. Handlers per seat (the 067 P5a convention): Telegram (and the
console, which routes through it) after the owner gate, refused in a room; the
REPL through ``h_contributed``.

Imports nothing but ``core.verbs``.
"""
from core.verbs import Verb, register_verbs

ADOPT_VERBS: tuple = (
    Verb("/adopt", "work",
         "Make a job or goal Rob wrote your own, after you see what it will do"),
)

register_verbs(
    ADOPT_VERBS,
    {"telegram": {"/adopt": "surfaces.telegram.adopt_ops:adopt_verb"},
     "repl": {"/adopt": "cli.ui.commands.h_adopt:h_adopt"}},
    source="core.standing_verbs.adopt")
