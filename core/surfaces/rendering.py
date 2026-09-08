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
    # 030 L7a: Telegram HTML has no table tag — degrade a markdown table to a
    # monospace <pre> block instead of a wall of literal pipes on the phone.
    body = _stash_tables(body, _stash)

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


_TABLE_SEP_RE = re.compile(
    r"^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(\|[ \t]*:?-{3,}:?[ \t]*)*\|?[ \t]*$")


def _stash_tables(body: str, _stash) -> str:
    """Lift markdown table blocks (header line + |---| separator + rows) into
    stashed ``<pre>`` blocks. A lone line with pipes is prose, not a table."""
    lines = body.split("\n")
    out: List[str] = []
    i = 0
    while i < len(lines):
        if ("|" in lines[i] and i + 1 < len(lines) and "|" in lines[i + 1]
                and _TABLE_SEP_RE.match(lines[i + 1])):
            j = i + 2
            while j < len(lines) and "|" in lines[j]:
                j += 1
            block = "\n".join(lines[i:j])
            out.append(_stash(f"<pre>{_esc(block)}</pre>"))
            i = j
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


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


# --- 030 L6: construct-aware source splitting for the html flavor -------------
# split_text() cut MID-CONSTRUCT: a fenced block or **bold** span crossing the
# 4096 boundary matched the converter's regex in NEITHER chunk, so the owner
# received literal backticks/asterisks. The splitter below moves the cut before
# an open construct, or (for a code block bigger than one message) closes the
# fence at the cut and reopens it in the next chunk.

_INLINE_MARKERS = ("**", "~~", "`")
_PARTIAL_LINK_RE = re.compile(r"\[[^\]\n]*(?:\]\([^)\s\n]*)?$")


def _fence_state(head: str):
    """(inside_fence, lang) after scanning ``head`` for ``` delimiters, plus the
    index of the still-open fence (or -1)."""
    inside, lang, open_idx = False, "", -1
    for m in re.finditer(r"```[ \t]*([A-Za-z0-9_+.-]*)", head):
        if not inside:
            inside, lang, open_idx = True, (m.group(1) or ""), m.start()
        else:
            inside, lang, open_idx = False, "", -1
    return inside, lang, open_idx


def _adjust_inline_cut(rest: str, cut: int, limit: int):
    """A cut landing inside an open inline construct moves so the construct stays
    whole: after its CLOSING marker when the whole span fits ``limit``, else
    before its OPENING marker. None = no change needed."""
    head = rest[:cut]
    candidates = []
    for marker in _INLINE_MARKERS:
        if head.count(marker) % 2 == 1:
            idx = head.rfind(marker)
            close = rest.find(marker, idx + len(marker))
            end = close + len(marker) if close != -1 else -1
            if 0 < end <= limit:
                candidates.append(end)
            elif idx > 0:
                candidates.append(idx)
    m = _PARTIAL_LINK_RE.search(head)
    if m and not m.group(0).endswith(")"):
        close = rest.find(")", m.start() + 1)
        if 0 < close + 1 <= limit:
            candidates.append(close + 1)
        elif m.start() > 0:
            candidates.append(m.start())
    if not candidates:
        return None
    return min(candidates)


def split_markdown(text: str, limit: int) -> List[str]:
    """Split markdown SOURCE into <=limit chunks so every chunk renders whole
    constructs (fences, bold, strike, inline code, links)."""
    if text == "":
        return [""]
    limit = max(8, int(limit))
    chunks: List[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        head = rest[:cut]
        inside, lang, open_idx = _fence_state(head)
        if inside:
            if open_idx > limit // 4:
                # Cut BEFORE the fence opens — the whole block rides the next chunk.
                cut = open_idx
                head = rest[:cut]
            else:
                # The block itself exceeds one message: close here, reopen next.
                cut = min(cut, limit - 4)  # room for the closing fence
                head = rest[:cut]
                chunks.append(head.rstrip("\n") + "\n```")
                rest = f"```{lang}\n" + rest[cut:].lstrip("\n")
                continue
        else:
            # Iterate: fixing one construct's cut can land inside another.
            for _ in range(4):
                adj = _adjust_inline_cut(rest, cut, limit)
                if adj is None or adj == cut:
                    break
                cut = adj
            head = rest[:cut]
        if cut <= 0:  # pathological input: accept a hard cut, never loop forever
            cut = limit
            head = rest[:cut]
        chunks.append(head)
        rest = rest[cut:]
    chunks.append(rest)
    return chunks


def _utf16_len(s: str) -> int:
    """Telegram meters message length in UTF-16 code units, not code points —
    astral-plane characters (most emoji) count double (030 L7b)."""
    return len(s) + sum(1 for ch in s if ord(ch) > 0xFFFF)


def _split_utf16_safe(text: str, limit: int, splitter) -> List[str]:
    """Chunks that fit ``limit`` in UTF-16 units. An oversize chunk (emoji-dense)
    re-splits at limit//2 code points, which guarantees the UTF-16 fit."""
    out: List[str] = []
    for c in splitter(text, limit):
        if _utf16_len(c) <= limit:
            out.append(c)
        else:
            out.extend(splitter(c, max(8, limit // 2)))
    return out


def render_for_flavor(text: str, flavor: str, limit: int) -> List[str]:
    body = text or ""
    limit = max(1, int(limit))
    if flavor == "html":
        # Split the SOURCE construct-aware (030 L6), then convert each chunk, so
        # neither a tag nor a markdown construct can straddle two messages.
        # Conversion only shrinks visible text (markers become markup, and
        # Telegram's cap is measured after entity parsing), so chunks still fit.
        return [markdown_to_html(c.strip("\n"))
                for c in _split_utf16_safe(body, limit, split_markdown)]
    if flavor == "markdown_v2":
        body = _escape_markdown_v2(body)
    return split_text(body, limit)
