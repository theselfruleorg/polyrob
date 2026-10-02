"""One concept, one owner word, in every seat (070 E.0).

The console, the command help, Telegram and the terminal each grew their own
word for the same thing — "session", "thread" and "conversation" for a chat;
"cap" and "ceiling" for a limit; "chain" and "on-chain" for a network. A person
who reads two seats then has to learn two products.

:data:`GLOSSARY` is that table as data. Each :class:`Term` names the one word,
the words also allowed, and the BANNED variants that owner copy may not use.
:func:`banned_hits` is the one matcher every check uses; the cross-seat ratchet
(``tests/unit/core/test_glossary_ratchet.py``) counts the strings with a hit in
each seat's corpus and holds every count at or under a ceiling that only falls.

Code is not owner prose: a backtick span, a ``{placeholder}``, usage grammar in
``<…>`` or ``[…]``, a slash verb with its first sub-verb (``/rail drop``) and a
``polyrob <command>`` run are stripped before the words are matched.

Three bans in the E.0 table depend on grammar a word match cannot see — "ask"
as a noun, "native" alone, "silent" as a state — and the brand ``POLYROB``; they
are review rules, not entries here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

__all__ = ["Term", "GLOSSARY", "banned_hits", "strip_code"]


@dataclass(frozen=True)
class Term:
    """One concept and its one owner word.

    ``key_scope`` limits the ban to copy keys with one of these prefixes (a
    word that is fine elsewhere, wrong in one place — ``action`` in a receipt).
    A scoped term never hits a string that has no key.
    """
    concept: str
    word: str
    allowed: Tuple[str, ...] = ()
    banned: Tuple[str, ...] = ()
    key_scope: Tuple[str, ...] = ()


GLOSSARY: Tuple[Term, ...] = (
    Term("a conversation with Rob", "chat", ("chat history",),
         ("session", "thread", "conversation")),
    Term("work Rob does by itself", "goal", ("group of goals",),
         ("task", "stream", "its own work")),
    Term("work at set times", "scheduled job", ("Schedule", "trading job", "posting job"),
         ("cron", "rail", "on a clock", "the clock")),
    Term("something the owner decides", "decision",
         ("approval", "question", "quote", "choice", "suggested change"),
         ("card", "pending", "proposal")),
    Term("what Rob owns", "holdings",
         ("Rob's records", "Rob's wallet", "tokens", "pool deposits"),
         ("book", "ledger", "position", "treasury")),
    Term("stop until later", "pause", ("resume", "turned off"),
         ("halt", "switched off")),
    Term("end one thing now", "stop", ("remove", "cancel", "delete"),
         ("kill", "drop")),
    Term("a money ceiling", "limit",
         ("daily limit", "limit per payment", "spend-without-asking limit"),
         ("cap", "ceiling")),
    Term("a blockchain", "network", ("Base", "Ethereum", "Solana", "the network's own coin"),
         ("chain", "on-chain")),
    Term("the agent", "Rob", ("I",), ("the agent", "instance")),
    Term("a worker Rob starts", "helper", (),
         ("worker", "profile", "sub-agent", "delegation")),
    Term("what Rob can do", "abilities",
         ("built in", "optional", "skill", "connected app"),
         ("capability", "capabilities", "tool", "tool id", "MCP", "procedure")),
    Term("a permission level", "permissions", ("yes", "no", "asks first"),
         ("posture", "mode", "axis", "regime", "autonomy", "autonomous")),
    Term("settings", "settings", (), ("preferences", "prefs", "config", "flags")),
    Term("files", "files", ("Ready for you", "Rob's working files", "From you"),
         ("workspace", "folder", "artifact")),
    Term("one unit of work in a chat", "step", (), ("action",),
         key_scope=("chat.receipt.", "chat.act.", "workpane.timeline_", "work.log.")),
    Term("the cost to run Rob", "AI cost", ("AI requests", "AI credit"),
         ("running cost", "runtime", "compute", "model calls")),
    Term("where the owner acts", "this console", ("Telegram", "the terminal", "other users"),
         ("seat", "tenant", "surface")),
    Term("a Telegram group Rob is in", "group", (), ("room", "channel", "supergroup")),
    Term("a person Rob talks to who is not the owner", "person", ("people",),
         ("correspondent", "contact", "counterparty")),
    Term("another agent or a program that calls Rob", "other agents and apps", (),
         ("A2A", "client", "caller")),
)

_BACKTICKS = re.compile(r"`[^`]*`")
_PLACEHOLDER = re.compile(r"\{[^{}]*\}")
_GRAMMAR = re.compile(r"<[^<>]*>|\[[^\[\]]*\]")
_SLASH_VERB = re.compile(r"(?<![\w/])/[a-z_]+(?: [a-z_]+)?")
_COMMAND = re.compile(r"\bpolyrob(?: [a-z][a-z_-]*){1,3}")


def strip_code(text: str) -> str:
    """*text* with every code-shaped run removed (see the module docstring)."""
    text = _BACKTICKS.sub(" ", text)
    text = _PLACEHOLDER.sub(" ", text)
    text = _GRAMMAR.sub(" ", text)
    text = _SLASH_VERB.sub(" ", text)
    return _COMMAND.sub(" ", text)


def _pattern(variant: str) -> re.Pattern:
    words = r"\s+".join(re.escape(w) for w in variant.split())
    return re.compile(rf"(?<![\w-]){words}(?:s|es)?(?![\w-])", re.IGNORECASE)


_COMPILED = tuple((term, tuple((v, _pattern(v)) for v in term.banned))
                  for term in GLOSSARY)


def banned_hits(text: str, key: Optional[str] = None) -> list:
    """The banned variants *text* uses, in glossary order, each once.

    *key* is the copy key the text belongs to; it decides whether a scoped term
    (``Term.key_scope``) applies.
    """
    clean = strip_code(text or "")
    out = []
    for term, patterns in _COMPILED:
        if term.key_scope and not (key and key.startswith(term.key_scope)):
            continue
        for variant, pattern in patterns:
            if variant not in out and pattern.search(clean):
                out.append(variant)
    return out
