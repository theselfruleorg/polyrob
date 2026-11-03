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
    # 030 L7a: Telegram HTML has no table tag. A narrow table (<= 3 columns)
    # becomes "label: value" lines a phone can read; a wider one degrades to a
    # monospace <pre> block instead of a wall of literal pipes (096 W6).
    body = _stash_tables(body, _stash)

    body = _esc(body)

    # 096 W6: a `---` rule becomes a thin line, not a literal run of dashes.
    body = _RULE_RE.sub(_THIN_RULE, body)
    # A heading is already bold, so drop any inner ** to avoid nesting <b> in <b>.
    body = _HEADING_RE.sub(lambda m: f"<b>{m.group(1).replace('**', '')}</b>", body)
    body = _BULLET_RE.sub(r"\1• ", body)
    # 096 W6: a numbered item keeps its own number ("1)" reads as "1.").
    body = _NUMBERED_RE.sub(r"\1\2. ", body)

    def _inline(seg: str) -> str:
        # Stash the whole anchor: a URL is not prose, so no emphasis rule may
        # touch it (".../_foo_" would otherwise become an italic tag inside the href).
        def link(m):
            from urllib.parse import urlsplit
            try:
                url = urlsplit(_html.unescape(m.group(2)))
                allowed = ((url.scheme.lower() in {'http', 'https'} and bool(url.netloc))
                           or (url.scheme.lower() == 'mailto' and '@' in url.path))
            except ValueError:
                allowed = False
            if not allowed:
                return m.group(1)  # no tg:// user mentions or application deep links
            return _stash(f'<a href="{_attr(m.group(2))}">{m.group(1)}</a>')
        seg = _LINK_RE.sub(link, seg)
        seg = _BOLD_RE.sub(r"<b>\1</b>", seg)
        seg = _BOLD_US_RE.sub(r"<b>\1</b>", seg)
        seg = _STRIKE_RE.sub(r"<s>\1</s>", seg)
        seg = _ITALIC_RE.sub(r"<i>\1</i>", seg)
        return _ITALIC_US_RE.sub(r"<i>\1</i>", seg)

    # 096 W6: `>` quote lines become a <blockquote>. Inline formatting runs per
    # block, so a bold span can never open inside a quote and close outside it.
    parts: List[str] = []
    for is_quote, seg in _quote_blocks(body):
        if is_quote:
            parts.append(f"<blockquote>{_inline(seg)}</blockquote>")
        else:
            parts.append(_inline(seg))
    body = "\n".join(parts)

    def _restore(m: re.Match) -> str:
        i = int(m.group(1))
        # Out of range == a \x00 that came from the source text, not from us.
        return stash[i] if i < len(stash) else m.group(0)

    return re.sub(r"\x00(\d+)\x00", _restore, body)


#: A markdown thematic break (``---`` / ``***`` / ``___``), on its own line.
_RULE_RE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$", re.MULTILINE)
_THIN_RULE = "──────────"
_NUMBERED_RE = re.compile(r"^([ \t]*)(\d{1,3})[.)][ \t]+", re.MULTILINE)
# After escaping, a quote marker `>` reads `&gt;`.
_QUOTE_LINE_RE = re.compile(r"^[ \t]*&gt;[ \t]?(.*)$")


def _quote_blocks(body: str):
    """``[(is_quote, text)]`` — runs of ``>`` lines (marker stripped) and the
    text between them, in order. Joined back with newlines by the caller."""
    blocks: List[tuple] = []
    for line in body.split("\n"):
        m = _QUOTE_LINE_RE.match(line)
        is_quote = m is not None
        text = m.group(1) if m else line
        if blocks and blocks[-1][0] == is_quote:
            blocks[-1][1].append(text)
        else:
            blocks.append((is_quote, [text]))
    return [(q, "\n".join(lines)) for q, lines in blocks]


_TABLE_SEP_RE = re.compile(
    r"^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(\|[ \t]*:?-{3,}:?[ \t]*)*\|?[ \t]*$")


#: A table this narrow reads as "label: value" lines on a phone (096 W6).
NARROW_TABLE_MAX_COLS = 3


def _table_cells(line: str) -> List[str]:
    cells = line.strip()
    if cells.startswith("|"):
        cells = cells[1:]
    if cells.endswith("|"):
        cells = cells[:-1]
    return [c.strip() for c in cells.split("|")]


def _narrow_table_lines(header: List[str], rows: List[List[str]]) -> List[str]:
    """``label: value`` lines (markdown source — it is escaped and formatted
    with the prose around it). The header names the columns once, in bold."""
    def line(cells: List[str], bold: bool) -> str:
        cells = cells + [""] * (len(header) - len(cells))
        label = cells[0]
        rest = " · ".join(c for c in cells[1:len(header)] if c)
        if bold and label:
            label = f"**{label}**"
        return f"{label}: {rest}" if rest else label
    out = [line(header, True)] if any(header) else []
    out.extend(line(r, False) for r in rows)
    return out


def _stash_tables(body: str, _stash) -> str:
    """Lift markdown table blocks (header line + |---| separator + rows). A
    table of <= ``NARROW_TABLE_MAX_COLS`` columns becomes "label: value" lines;
    a wider one a stashed ``<pre>`` block. A lone line with pipes is prose."""
    lines = body.split("\n")
    out: List[str] = []
    i = 0
    while i < len(lines):
        if ("|" in lines[i] and i + 1 < len(lines) and "|" in lines[i + 1]
                and _TABLE_SEP_RE.match(lines[i + 1])):
            j = i + 2
            while j < len(lines) and "|" in lines[j]:
                j += 1
            header = _table_cells(lines[i])
            if len(header) <= NARROW_TABLE_MAX_COLS:
                out.extend(_narrow_table_lines(
                    header, [_table_cells(r) for r in lines[i + 2:j]]))
            else:
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
                    adj = _italic_cut(rest, cut, limit)
                if adj is None or adj == cut:
                    break
                cut = adj
            head = rest[:cut]
        if cut <= 0:  # pathological input: accept a hard cut, never loop forever
            cut = limit
            head = rest[:cut]
        chunks.append(head)
        rest = rest[cut:]
        # 096 W6: a table cut in two keeps its header in the second half, so
        # the rows there still render as a table, not as literal pipes.
        carry = _table_header_carry(head, rest)
        if carry and len(carry) < limit // 2:
            rest = carry + rest.lstrip("\n")
    chunks.append(rest)
    return chunks


def _table_header_carry(head: str, rest: str) -> str:
    """``header\n|---|\n`` when ``head`` ends inside a table that ``rest``
    continues, else ``""``."""
    nxt = rest.lstrip("\n").split("\n", 1)[0]
    if "|" not in nxt:
        return ""
    lines = head.rstrip("\n").split("\n")
    i = len(lines) - 1
    while i >= 1 and "|" in lines[i]:
        if _TABLE_SEP_RE.match(lines[i]) and "|" in lines[i - 1]:
            return f"{lines[i - 1]}\n{lines[i]}\n"
        i -= 1
    return ""


def _italic_cut(rest: str, cut: int, limit: int):
    """A cut inside a one-line ``*italic*`` / ``_italic_`` span moves before the
    span (or after it, when the whole span fits). None = no change."""
    line_start = rest.rfind("\n", 0, cut) + 1
    line_end = rest.find("\n", cut)
    line = rest[line_start:line_end if line_end != -1 else len(rest)]
    at = cut - line_start
    for rx in (_ITALIC_RE, _ITALIC_US_RE):
        for m in rx.finditer(line):
            if m.start() < at < m.end():
                end = line_start + m.end()
                if end <= limit:
                    return end
                return line_start + m.start() if line_start + m.start() > 0 else None
    return None


_PLAIN_LINK_RE = re.compile(r"\[([^\]\n]*)\]\(([^)\s]+)\)")


def markdown_to_plain(text: str) -> str:
    """The agent's markdown as plain text — the fallback when a surface rejects
    the rendered HTML (096 W6). Strips the markers instead of sending them raw:
    fences and inline code keep their text, ``[t](url)`` reads ``t (url)``,
    ``**b**``/``*i*``/``~~s~~`` lose their markers, headings their ``#``,
    bullets read ``•``. Never raises; an empty input returns ``""``."""
    if not text:
        return ""
    try:
        stash: List[str] = []

        def _keep(literal: str) -> str:
            stash.append(literal)
            return f"\x00{len(stash) - 1}\x00"

        # Code is literal: lifted out first so no marker rule touches it.
        body = _FENCE_RE.sub(lambda m: _keep(m.group(2).strip("\n")), text)
        body = _INLINE_CODE_RE.sub(lambda m: _keep(m.group(1)), body)
        body = _PLAIN_LINK_RE.sub(
            lambda m: m.group(1) if m.group(1) == m.group(2)
            else f"{m.group(1)} ({m.group(2)})", body)
        body = _RULE_RE.sub(_THIN_RULE, body)
        body = _HEADING_RE.sub(lambda m: m.group(1).replace("**", ""), body)
        body = _BULLET_RE.sub(r"\1• ", body)
        body = _BOLD_RE.sub(r"\1", body)
        body = _BOLD_US_RE.sub(r"\1", body)
        body = _STRIKE_RE.sub(r"\1", body)
        body = _ITALIC_RE.sub(r"\1", body)
        body = _ITALIC_US_RE.sub(r"\1", body)
        return re.sub(r"\x00(\d+)\x00",
                      lambda m: stash[int(m.group(1))] if int(m.group(1)) < len(stash)
                      else m.group(0), body)
    except Exception:
        return text


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


def split_for_flavor(text: str, flavor: str, limit: int) -> List[str]:
    """The SOURCE chunks ``render_for_flavor`` renders — one per rendered chunk,
    cut by the SAME splitter. A surface's plain-text fallback for chunk *i* must
    be source chunk *i*; a different splitter (``split_text``) cut elsewhere and
    the fallback duplicated or dropped text (OB19)."""
    body = text or ""
    limit = max(1, int(limit))
    if flavor == "html":
        return [c.strip("\n") for c in _split_utf16_safe(body, limit, split_markdown)]
    return split_text(body, limit)


def render_for_flavor(text: str, flavor: str, limit: int) -> List[str]:
    body = text or ""
    limit = max(1, int(limit))
    if flavor == "html":
        # Split the SOURCE construct-aware (030 L6), then convert each chunk, so
        # neither a tag nor a markdown construct can straddle two messages.
        # Conversion only shrinks visible text (markers become markup, and
        # Telegram's cap is measured after entity parsing), so chunks still fit.
        return [markdown_to_html(c) for c in split_for_flavor(body, flavor, limit)]
    if flavor == "markdown_v2":
        body = _escape_markdown_v2(body)
    return split_text(body, limit)
