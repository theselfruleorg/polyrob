"""The ONE ``/kb`` grammar every owner seat parses (audit 2026-10-03, CLI7).

Before this, Telegram read ``/kb <words>`` as a SEARCH and a bare ``/kb`` as a
usage error, while the REPL read ``/kb <word>`` as "list that collection" and
a bare ``/kb`` as a list. The same line meant two different things depending
on the seat it was typed on.

Grammar::

    /kb                     list the sources (every collection)
    /kb list [collection]   list the sources (one collection when named)
    /kb search <query>      search the knowledge base
    /kb <words>             search (the bare form)

Pure: no I/O, no imports above ``core``. The seats run the reads.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

LIST = "list"
SEARCH = "search"
USAGE = "usage"

KB_USAGE = ("Usage: /kb — list the knowledge base · /kb list [collection] · "
            "/kb search <query> · /kb <words> (search)")


@dataclass(frozen=True)
class KbCommand:
    action: str                       # LIST | SEARCH | USAGE
    collection: Optional[str] = None  # LIST only; None = every collection
    query: str = ""                   # SEARCH only


def parse_kb_args(args: List[str]) -> KbCommand:
    """The words after ``/kb`` -> what the owner asked for."""
    words = [str(a) for a in (args or []) if str(a).strip()]
    if not words:
        return KbCommand(LIST)
    head = words[0].lower()
    if head == "list":
        if len(words) > 2:
            return KbCommand(USAGE)
        return KbCommand(LIST, collection=words[1] if len(words) == 2 else None)
    if head == "search":
        query = " ".join(words[1:]).strip()
        return KbCommand(SEARCH, query=query) if query else KbCommand(USAGE)
    return KbCommand(SEARCH, query=" ".join(words).strip())


__all__ = ["KB_USAGE", "KbCommand", "LIST", "SEARCH", "USAGE", "parse_kb_args"]
