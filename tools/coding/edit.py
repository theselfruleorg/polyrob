"""Pure str_replace logic for the coding tool (C1).

Exact-match, unique-or-fail: a 0-match or ambiguous (>1) replace FAILS LOUDLY
rather than silently editing the wrong place. ``replace_all`` opts into replacing
every occurrence. No file I/O here — the tool layer owns read/write + path
confinement; this stays a pure, trivially-testable string transform.

I-8 whitespace-tolerant fallback: when the single-replace path (``replace_all``
False) finds ZERO exact matches, a second rung tries a whitespace-normalized,
line-wise search — each line of ``old_string`` and of ``content`` is compared
with ``.strip()`` applied, so indentation drift and leading/trailing whitespace
differences don't block a match. The normalized search still must resolve to a
UNIQUE window or it raises ``EditError`` (0 matches -> "not found"; >=2 matches
-> "not unique after whitespace normalization") — it never silently guesses
among candidates. On a unique normalized match, ``new_string`` is re-indented
by a single delta derived from the FIRST line only (the character-count
difference between the matched span's real leading whitespace and
``old_string``'s own first-line leading whitespace), applied uniformly to every
line of ``new_string``. This is a deliberate simplification: it is exact for
the common case of a uniform indent shift, but does not attempt a full
per-line semantic re-indent if interior lines of a multi-line ``old_string``
carry their own, different relative indentation. ``replace_all`` stays
exact-match-only; the normalized fallback never runs for it.
"""

# NOTE: deliberately NO ``from __future__ import annotations`` — keep this module
# consistent with the coding-tool package landmine rule (registry param-model
# introspection). It costs nothing here.

import re as _re


class EditError(Exception):
    """Raised when a str_replace cannot be applied unambiguously."""


def apply_str_replace(
    content: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """Return ``content`` with ``old_string`` replaced by ``new_string``.

    Raises :class:`EditError` when ``old_string`` is absent, identical to
    ``new_string``, or non-unique without ``replace_all``.
    """
    if old_string == new_string:
        raise EditError("old_string and new_string are identical; nothing to change")
    count = content.count(old_string)
    if count == 0:
        if replace_all:
            # Normalization is single-replace-path only (see module docstring).
            raise EditError("old_string not found in file")
        return _apply_normalized_fallback(content, old_string, new_string)
    if count > 1 and not replace_all:
        raise EditError(
            f"old_string is not unique ({count} matches) — add surrounding context "
            f"to make it unique, or pass replace_all=true"
        )
    if replace_all:
        return content.replace(old_string, new_string)
    return content.replace(old_string, new_string, 1)


def _apply_normalized_fallback(content: str, old_string: str, new_string: str) -> str:
    """Whitespace-normalized, line-wise fallback used when the exact-match
    rung finds zero occurrences. See the module docstring for the full
    contract (unique-or-fail; single-delta re-indent from line 1).

    Known false-negative: if ``old_string`` ends with ``"\\n"``, its
    ``.split("\\n")`` gains a trailing empty-string "line", so the window
    search also requires ``content`` to have a matching blank line
    immediately after the block. Without one, the window simply won't match
    (0 matches -> loud "not found"), even though the meaningful lines line
    up — it never silently drops the trailing empty element to compensate.
    """
    old_lines = old_string.split("\n")
    content_lines = content.split("\n")
    n = len(old_lines)
    stripped_old = [line.strip() for line in old_lines]
    stripped_content = [line.strip() for line in content_lines]
    matches = [
        i
        for i in range(len(stripped_content) - n + 1)
        if stripped_content[i : i + n] == stripped_old
    ]
    if not matches:
        raise EditError("old_string not found in file")
    if len(matches) > 1:
        raise EditError(
            f"old_string is not unique after whitespace normalization "
            f"({len(matches)} matches) — add surrounding context to make it unique"
        )
    idx = matches[0]
    orig_indent = _leading_ws(content_lines[idx])
    old_indent = _leading_ws(old_lines[0])
    reindented = _reindent_by_delta(new_string, orig_indent, old_indent)
    new_lines = reindented.split("\n")
    result_lines = content_lines[:idx] + new_lines + content_lines[idx + n :]
    return "\n".join(result_lines)


def _leading_ws(line: str) -> str:
    """Return the leading-whitespace prefix of a single line."""
    return line[: len(line) - len(line.lstrip())]


def _reindent_by_delta(new_string: str, orig_indent: str, old_indent: str) -> str:
    """Apply the leading-whitespace delta between ``orig_indent`` (the
    matched span's real first-line indent) and ``old_indent`` (``old_string``'s
    own first-line indent) uniformly to every line of ``new_string``.

    Growing (``orig_indent`` longer): prepend the extra suffix of
    ``orig_indent`` to every non-blank line. Shrinking: strip up to that many
    leading whitespace characters (bounded by what each line actually has,
    never touching non-whitespace content) from every line.
    """
    delta = len(orig_indent) - len(old_indent)
    if delta == 0:
        return new_string
    lines = new_string.split("\n")
    if delta > 0:
        pad = orig_indent[-delta:]
        lines = [pad + line if line else line for line in lines]
    else:
        strip_n = -delta
        lines = [_strip_leading_ws(line, strip_n) for line in lines]
    return "\n".join(lines)


def _strip_leading_ws(line: str, n: int) -> str:
    """Strip up to ``n`` leading-whitespace characters from ``line``, never
    removing more than the whitespace actually present.
    """
    actual_ws_len = len(line) - len(line.lstrip())
    remove = min(n, actual_ws_len)
    return line[remove:]


_HUNK_RE = _re.compile(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")


def apply_patch(content: str, patch: str) -> str:
    """Apply a unified-diff ``patch`` to ``content`` and return the new text.

    Reject-on-context-mismatch: every context (' ') and removed ('-') line must
    match the source at the hunk location, or :class:`EditError` is raised. The
    ``@@ -start,len +start,len @@`` header locates the apply point (1-based).
    Lines starting with '+' are inserted; '\\ No newline...' and blank trailing
    lines are ignored. Single-file only (the tool layer owns one file per call).
    """
    original = content.split("\n")
    patch_lines = patch.split("\n")
    result: list[str] = []
    src_idx = 0
    i = 0
    saw_hunk = False
    while i < len(patch_lines):
        header = _HUNK_RE.match(patch_lines[i])
        if not header:
            i += 1
            continue
        saw_hunk = True
        start = int(header.group(1)) - 1  # 0-based
        if start < src_idx:
            raise EditError("overlapping or out-of-order hunks")
        if start > len(original):
            raise EditError(f"hunk start {start + 1} is beyond end of file")
        result.extend(original[src_idx:start])
        src_idx = start
        i += 1
        while i < len(patch_lines) and not _HUNK_RE.match(patch_lines[i]):
            pl = patch_lines[i]
            if pl.startswith("\\"):  # "\ No newline at end of file"
                i += 1
                continue
            if pl.startswith(" "):
                if src_idx >= len(original) or original[src_idx] != pl[1:]:
                    found = original[src_idx] if src_idx < len(original) else "<EOF>"
                    raise EditError(
                        f"context mismatch at line {src_idx + 1}: "
                        f"expected {pl[1:]!r}, found {found!r}"
                    )
                result.append(original[src_idx])
                src_idx += 1
            elif pl.startswith("-"):
                if src_idx >= len(original) or original[src_idx] != pl[1:]:
                    found = original[src_idx] if src_idx < len(original) else "<EOF>"
                    raise EditError(
                        f"removed-line mismatch at line {src_idx + 1}: "
                        f"expected {pl[1:]!r}, found {found!r}"
                    )
                src_idx += 1
            elif pl.startswith("+"):
                result.append(pl[1:])
            # bare "" (patch tail) and any other line: ignore
            i += 1
    if not saw_hunk:
        raise EditError("no @@ hunk header found in patch")
    result.extend(original[src_idx:])
    return "\n".join(result)
