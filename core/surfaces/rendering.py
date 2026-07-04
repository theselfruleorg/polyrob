"""Shared outbound rendering: per-flavor escaping + size-aware splitting. One home so
every surface declares capabilities.markdown_flavor and gets correct escaping + chunking
for free (generalizes surfaces/telegram/markdown.py). Splitting prefers a newline, then a
space, then a hard cut — and for markdown_v2 never cuts in the middle of a '\\x' escape."""
from typing import List

_MD_V2_RESERVED = r"_*[]()~`>#+-=|{}.!"


def _escape_markdown_v2(text: str) -> str:
    out = []
    for ch in text:
        if ch in _MD_V2_RESERVED:
            out.append("\\")
        out.append(ch)
    return "".join(out)


def _split(text: str, limit: int) -> List[str]:
    if text == "":
        return [""]
    chunks: List[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        # never end a chunk on an odd-length run of trailing backslashes
        # (an even run = fully-escaped pairs, which is fine; odd = dangling escape)
        trail_start = cut
        while trail_start > 0 and rest[trail_start - 1] == "\\":
            trail_start -= 1
        trailing_bs = cut - trail_start
        if trailing_bs % 2 == 1:
            if cut - 1 >= 1:
                cut -= 1
            else:
                cut = limit  # pathological: window is all backslashes; accept hard cut
        chunks.append(rest[:cut])
        rest = rest[cut:]
    chunks.append(rest)
    return chunks


def render_for_flavor(text: str, flavor: str, limit: int) -> List[str]:
    body = text or ""
    if flavor == "markdown_v2":
        body = _escape_markdown_v2(body)
    # "html"/"none" pass through (HTML escaping is surface-specific; add when a surface needs it)
    return _split(body, max(1, int(limit)))
