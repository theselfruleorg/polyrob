"""Obsidian-style ``[[wikilink]]`` parsing for the notes substrate (C1, 2026-07-11)."""
import re

_WIKILINK_RE = re.compile(r"\[\[([^\]\[]+)\]\]")


def parse_wikilinks(text) -> list:
    """Extract [[wikilink]] targets from a note body, in order. '' / None -> []."""
    if not text:
        return []
    return _WIKILINK_RE.findall(str(text))
