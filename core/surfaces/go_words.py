"""The ONE set of words that turn a quoting money verb into an executing one.

A leaf (no imports), so a module that only needs the word check — the reply-button
store ``core.surfaces.nav`` — reads it without joining the cards import cycle.
``core.surfaces.cards`` re-exports both names.
"""
from typing import List

GO_WORDS = ("go", "execute", "confirm")


def is_go_word(word: str) -> bool:
    """True when *word* is one of :data:`GO_WORDS` (any case)."""
    return str(word or "").lower() in GO_WORDS


def executes(args: List[str]) -> bool:
    """True when these words would EXECUTE rather than quote: a go word ANYWHERE."""
    return any(is_go_word(a) for a in (args or []))
