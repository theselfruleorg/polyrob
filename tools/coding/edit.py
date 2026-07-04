"""Pure str_replace logic for the coding tool (C1).

Exact-match, unique-or-fail: a 0-match or ambiguous (>1) replace FAILS LOUDLY
rather than silently editing the wrong place. ``replace_all`` opts into replacing
every occurrence. No file I/O here — the tool layer owns read/write + path
confinement; this stays a pure, trivially-testable string transform.
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
        raise EditError("old_string not found in file")
    if count > 1 and not replace_all:
        raise EditError(
            f"old_string is not unique ({count} matches) — add surrounding context "
            f"to make it unique, or pass replace_all=true"
        )
    if replace_all:
        return content.replace(old_string, new_string)
    return content.replace(old_string, new_string, 1)


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
