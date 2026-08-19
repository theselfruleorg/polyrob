"""Shared outbound rendering: per-flavor conversion + size-aware splitting. One home so
every surface declares capabilities.markdown_flavor and gets correct formatting + chunking
for free. Splitting prefers a newline, then a space, then a hard cut — and for markdown_v2
never cuts in the middle of a '\\x' escape.

Flavors:
  "none"        - pass through (Discord/Slack render common markdown themselves)
  "html"        - agent markdown -> Telegram HTML (the ONE converter; see markdown_to_html)
  "markdown_v2" - escape every reserved char (renders literally; kept for callers that
                  want a guaranteed-safe, formatting-free payload)
"""
import html as _html
import re
from typing import List

_MD_V2_RESERVED = r"_*[]()~`>#+-=|{}.!"

# --- markdown -> Telegram HTML ------------------------------------------------
# Telegram's HTML parse mode accepts a small tag set (b/i/u/s/a/code/pre/blockquote).
# It is far more robust than MarkdownV2: only & < > need escaping, so a stray markdown
# char in agent prose can't corrupt the message.

_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+.-]*)[ \t]*\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_BULLET_RE = re.compile(r"^([ \t]*)[-*+][ \t]+", re.MULTILINE)
_LINK_RE = re.compile(r"\[([^\]\n]*)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(?!\s)(.+?)(?<!\s)\*\*", re.DOTALL)
_BOLD_US_RE = re.compile(r"(?<!\w)__(?!\s)(.+?)(?<!\s)__(?!\w)", re.DOTALL)
_STRIKE_RE = re.compile(r"~~(?!\s)(.+?)(?<!\s)~~", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<![*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)")
# `_` italic only at word boundaries, so snake_case identifiers survive intact.
_ITALIC_US_RE = re.compile(r"(?<!\w)_(?!\s)([^_\n]+?)(?<!\s)_(?!\w)")


def markdown_to_html(text: str) -> str:
    """Convert the markdown an LLM writes into the HTML subset Telegram renders.

    Code spans/blocks are lifted out first so markdown inside them stays literal;
    everything else is HTML-escaped before any tag is emitted, so user text can never
    inject markup. Unsupported constructs degrade to plain text rather than raising.
    """
    if not text:
        return ""

    stash: List[str] = []

    def _stash(rendered: str) -> str:
        stash.append(rendered)
        return f"\x00{len(stash) - 1}\x00"

    def _fence(m: re.Match) -> str:
        lang, body = m.group(1), _esc(m.group(2).strip("\n"))
        inner = f'<code class="language-{lang}">{body}</code>' if lang else body
        return _stash(f"<pre>{inner}</pre>")

    body = _FENCE_RE.sub(_fence, text)
    body = _INLINE_CODE_RE.sub(lambda m: _stash(f"<code>{_esc(m.group(1))}</code>"), body)

    body = _esc(body)

    # A heading is already bold, so drop any inner ** to avoid nesting <b> in <b>.
    body = _HEADING_RE.sub(lambda m: f"<b>{m.group(1).replace('**', '')}</b>", body)
    body = _BULLET_RE.sub(r"\1• ", body)
    # Stash the whole anchor: a URL is not prose, so no emphasis rule may touch it
    # (".../_foo_" would otherwise become an italic tag inside the href).
    body = _LINK_RE.sub(lambda m: _stash(f'<a href="{_attr(m.group(2))}">{m.group(1)}</a>'), body)
    body = _BOLD_RE.sub(r"<b>\1</b>", body)
    body = _BOLD_US_RE.sub(r"<b>\1</b>", body)
    body = _STRIKE_RE.sub(r"<s>\1</s>", body)
    body = _ITALIC_RE.sub(r"<i>\1</i>", body)
    body = _ITALIC_US_RE.sub(r"<i>\1</i>", body)

    def _restore(m: re.Match) -> str:
        i = int(m.group(1))
        # Out of range == a \x00 that came from the source text, not from us.
        return stash[i] if i < len(stash) else m.group(0)

    return re.sub(r"\x00(\d+)\x00", _restore, body)


def _attr(url: str) -> str:
    """Quote a URL for use inside an href (it is already & < > escaped)."""
    return url.replace('"', "&quot;").replace("'", "&#x27;")


def _esc(text: str) -> str:
    return _html.escape(text, quote=False)


def _escape_markdown_v2(text: str) -> str:
    out = []
    for ch in text:
        if ch in _MD_V2_RESERVED:
            out.append("\\")
        out.append(ch)
    return "".join(out)


def split_text(text: str, limit: int) -> List[str]:
    """Split into <=limit chunks, preferring a newline then a space boundary. The ONE
    splitter every surface uses."""
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
    limit = max(1, int(limit))
    if flavor == "html":
        # Split the SOURCE, then convert each chunk, so a tag can never straddle two
        # messages. Conversion only shrinks visible text (markers become markup, and
        # Telegram's cap is measured after entity parsing), so chunks still fit.
        return [markdown_to_html(c.strip("\n")) for c in split_text(body, limit)]
    if flavor == "markdown_v2":
        body = _escape_markdown_v2(body)
    return split_text(body, limit)
