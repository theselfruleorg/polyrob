"""Pure str_replace logic for the coding tool (C1).

Exact-match, unique-or-fail: a 0-match or ambiguous (>1) replace FAILS LOUDLY
rather than silently editing the wrong place. ``replace_all`` opts into replacing
every occurrence. No file I/O here — the tool layer owns read/write + path
confinement; this stays a pure, trivially-testable string transform.

T2.2 fuzzy-edit rung ladder: when the single-replace path (``replace_all``
False) finds ZERO exact matches, ``apply_str_replace_ex`` tries progressively
looser line-wise window searches, strictest first, and reports which rung
matched:

1. ``exact`` — byte-identical substring match (no fallback needed).
2. ``whitespace`` — each line of ``old_string`` and of ``content`` is compared
   with ``.strip()`` applied, so indentation drift and leading/trailing
   whitespace differences don't block a match.
3. ``blank-edge`` — only attempted when ``old_string`` has leading/trailing
   BLANK lines (e.g. a trailing ``"\n"`` that makes ``.split("\n")`` gain a
   trailing empty-string "line"); those blank edge lines are trimmed from the
   search window and rung 2's per-line ``.strip()`` search is retried. Fixes
   the false-negative where a block's meaningful lines line up but a
   trailing/leading blank line in ``old_string`` doesn't have a matching
   blank line in ``content``.
4. ``interior-whitespace`` — each line is normalized with
   ``" ".join(line.split())``, collapsing interior whitespace runs (e.g.
   ``a  =  1`` vs ``a = 1``) in addition to edge-stripping.

Every rung is UNIQUE-OR-FAIL: 0 candidates at a rung falls through to the
next rung; >=2 candidates at any rung raises ``EditError`` immediately (never
guesses, and never loosens further to try to disambiguate). If every rung
finds 0 candidates, the final error is byte-identical to the original
"old_string not found in file" message — no information about which rungs
were tried leaks into it. On a unique match, ``new_string`` is re-indented by
a single delta derived from the FIRST line only (the character-count
difference between the matched span's real leading whitespace and the
matched window's own first-line leading whitespace), applied uniformly to
every line of ``new_string``. This is a deliberate simplification: it is
exact for the common case of a uniform indent shift, but does not attempt a
full per-line semantic re-indent if interior lines of a multi-line
``old_string`` carry their own, different relative indentation. For the
blank-edge rung, ``new_string`` is used as-is — its own blank edges are the
author's intent, not normalized away.

``replace_all`` stays exact-match-only; NONE of the fuzzy rungs ever run for
it. ``apply_str_replace`` is a thin wrapper over ``apply_str_replace_ex`` that
returns just the new content, for callers that don't need the matched rung.
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

    Thin wrapper over :func:`apply_str_replace_ex` for callers that don't
    need to know which rung matched. Raises :class:`EditError` when
    ``old_string`` is absent, identical to ``new_string``, or non-unique
    without ``replace_all``.
    """
    return apply_str_replace_ex(content, old_string, new_string, replace_all)[0]


def apply_str_replace_ex(
    content: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
):
    """Return ``(new_content, rung)`` — ``content`` with ``old_string``
    replaced by ``new_string``, plus the name of the ladder rung that
    matched (``"exact"``, ``"whitespace"``, ``"blank-edge"``, or
    ``"interior-whitespace"``). See the module docstring for the full ladder
    contract (unique-or-fail per rung, strictest first).

    Raises :class:`EditError` when ``old_string`` is absent, identical to
    ``new_string``, or non-unique without ``replace_all``.
    """
    if old_string == new_string:
        raise EditError("old_string and new_string are identical; nothing to change")
    count = content.count(old_string)
    if count == 0:
        if replace_all:
            # Fuzzy rungs are single-replace-path only (see module docstring).
            raise EditError("old_string not found in file")
        return _apply_fuzzy_ladder(content, old_string, new_string)
    if count > 1 and not replace_all:
        raise EditError(
            f"old_string is not unique ({count} matches) — add surrounding context "
            f"to make it unique, or pass replace_all=true"
        )
    if replace_all:
        return content.replace(old_string, new_string), "exact"
    return content.replace(old_string, new_string, 1), "exact"


def _apply_fuzzy_ladder(content: str, old_string: str, new_string: str):
    """Run rungs 2-4 (whitespace, blank-edge, interior-whitespace) in order
    against ``content``, used when the exact-match rung finds zero
    occurrences. Returns ``(new_content, rung)`` on the first rung with a
    unique hit; raises :class:`EditError` immediately on an ambiguous hit at
    any rung (never guesses); if every rung finds zero candidates, raises the
    same "old_string not found in file" message the pre-ladder code did.
    """
    content_lines = content.split("\n")
    old_lines = old_string.split("\n")

    result = _try_window_rung(
        content_lines, old_lines, new_string, _strip_ws, "whitespace",
        "after whitespace normalization",
    )
    if result is not None:
        return result

    # Rung 3 only applies when old_string actually has blank leading/trailing
    # lines to trim (e.g. a trailing "\n"); new_string is used as-is — its
    # own blank edges are the author's intent, not normalized away.
    trimmed = _trim_blank_edge_lines(old_lines)
    if trimmed and trimmed != old_lines:
        result = _try_window_rung(
            content_lines, trimmed, new_string, _strip_ws, "blank-edge",
            "after blank-edge trim",
        )
        if result is not None:
            return result

    result = _try_window_rung(
        content_lines, old_lines, new_string, _collapse_interior_ws,
        "interior-whitespace", "after interior-whitespace collapse",
    )
    if result is not None:
        return result

    raise EditError("old_string not found in file")


def _try_window_rung(content_lines, target_lines, new_string, normalize, rung_name, fail_phrase):
    """Search ``content_lines`` for a unique window matching ``target_lines``
    under ``normalize``d per-line comparison.

    Returns ``(new_content, rung_name)`` on a unique hit, or ``None`` on zero
    candidates so the caller can fall through to the next rung. Raises
    :class:`EditError` immediately on an ambiguous (>=2 candidates) hit —
    ambiguity at any rung is a terminal failure, never guessed and never
    loosened further.
    """
    n = len(target_lines)
    if n == 0:
        return None
    normalized_target = [normalize(line) for line in target_lines]
    matches = [
        i
        for i in range(len(content_lines) - n + 1)
        if [normalize(line) for line in content_lines[i : i + n]] == normalized_target
    ]
    if not matches:
        return None
    if len(matches) > 1:
        raise EditError(
            f"old_string is not unique {fail_phrase} "
            f"({len(matches)} matches) — add surrounding context to make it unique"
        )
    idx = matches[0]
    orig_indent = _leading_ws(content_lines[idx])
    old_indent = _leading_ws(target_lines[0])
    reindented = _reindent_by_delta(new_string, orig_indent, old_indent)
    new_lines = reindented.split("\n")
    result_lines = content_lines[:idx] + new_lines + content_lines[idx + n :]
    return "\n".join(result_lines), rung_name


def _strip_ws(line: str) -> str:
    """Per-line normalizer for the ``whitespace``/``blank-edge`` rungs:
    strips leading/trailing whitespace only (interior runs untouched)."""
    return line.strip()


def _collapse_interior_ws(line: str) -> str:
    """Per-line normalizer for the ``interior-whitespace`` rung: collapses
    every run of whitespace (leading, trailing, and interior) to a single
    space."""
    return " ".join(line.split())


def _trim_blank_edge_lines(lines: list) -> list:
    """Return ``lines`` with leading/trailing blank (whitespace-only or
    empty) entries removed. Returns the list unchanged if there are none."""
    start, end = 0, len(lines)
    while start < end and lines[start].strip() == "":
        start += 1
    while end > start and lines[end - 1].strip() == "":
        end -= 1
    return lines[start:end]


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


_HUNK_RE = _re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")

# Coding-agent review B5 (2026-09-24): a hunk used to apply ONLY at the exact
# ``@@`` line and a bare ``""`` context line (models drop the leading space on
# a blank line) was skipped, so LLM-written patches failed. A hunk now applies
# at the NEAREST position its context matches, strictest rung first.
_PATCH_RUNGS = (
    ("exact", lambda line: line),
    ("trailing-whitespace", lambda line: line.rstrip()),
    ("whitespace", lambda line: line.strip()),
)


def _parse_hunks(patch: str) -> list:
    """``[(hint, ops)]`` — *hint* is the 0-based ``@@ -start`` line; *ops* is a
    list of ``(" "|"-"|"+", text)``. A bare ``""`` inside a hunk is a blank
    context line; trailing ones (the patch's own line ends) are dropped."""
    hunks = []
    ops = None
    for line in patch.split("\n"):
        header = _HUNK_RE.match(line)
        if header:
            ops = []
            start = int(header.group(1))
            # Unified-diff convention: "-N,0" (an insert-only hunk) means AFTER
            # line N, so its 0-based insert point is N, not N-1.
            if header.group(2) == "0":
                hunks.append((start, ops))
            else:
                hunks.append((max(start - 1, 0), ops))
            continue
        if ops is None or line.startswith("\\"):  # preamble / "\ No newline"
            continue
        if line.startswith("--- ") and ops and any(op != " " for op, _ in ops):
            raise EditError("the patch touches more than one file; send one file per call")
        if line == "":
            ops.append((" ", ""))
        elif line[0] in " -+":
            ops.append((line[0], line[1:]))
        else:
            raise EditError(f"unrecognised patch line: {line!r}")
    for _, hunk_ops in hunks:
        while hunk_ops and hunk_ops[-1] == (" ", ""):
            hunk_ops.pop()
    return hunks


def _locate(original: list, old: list, lo: int, hint: int):
    """``(position, rung)`` for *old* at or after *lo*.

    The ``@@`` position wins when the context matches there exactly. Otherwise
    the hunk RELOCATES only to a UNIQUE match (strictest rung first): a
    relocation between two candidates is a guess, and a guessed edit in the
    wrong place is worse than a refused one (review B5 follow-up).
    """
    if not old:
        return max(lo, min(hint, len(original))), "exact"
    if hint >= lo and original[hint:hint + len(old)] == old:
        return hint, "exact"
    for rung, norm in _PATCH_RUNGS:
        want = [norm(x) for x in old]
        normed = [norm(x) for x in original]
        hits = [pos for pos in range(lo, len(original) - len(old) + 1)
                if normed[pos:pos + len(old)] == want]
        if len(hits) == 1:
            return hits[0], rung
        if len(hits) > 1:
            lines = ", ".join(str(h + 1) for h in hits[:5])
            raise EditError(
                f"hunk context matches {len(hits)} places (lines {lines}) and not at its "
                f"@@ line {hint + 1}; add context lines or fix the @@ line")
    head = next((x for x in old if x.strip()), old[0])
    raise EditError(
        f"context mismatch: the hunk does not match near line {hint + 1} (first line {head!r}); "
        "re-read the file and regenerate the patch")


def apply_patch(content: str, patch: str) -> str:
    """Apply a unified-diff ``patch`` to ``content`` and return the new text.

    Each hunk's context (' ') and removed ('-') lines must match the source —
    at the ``@@ -start`` line or, failing that, at the ONE other place they
    match, trying exact, then trailing-whitespace, then whitespace-insensitive
    comparison. More than one candidate is refused, never guessed.
    Context lines keep the FILE's text (a fuzzy match never rewrites them).
    Hunks apply in order and may not overlap. Lines starting with '+' are
    inserted; '\\ No newline...' lines are ignored. Single-file only (the
    tool layer owns one file per call).
    """
    hunks = _parse_hunks(patch)
    if not hunks:
        raise EditError("no @@ hunk header found in patch")
    original = content.split("\n")
    result: list[str] = []
    src_idx = 0
    delta = 0  # how far earlier hunks moved the file (applied offset)
    for hint, ops in hunks:
        old = [text for op, text in ops if op in " -"]
        pos, _rung = _locate(original, old, src_idx, hint + delta)
        delta = pos - hint
        result.extend(original[src_idx:pos])
        cursor = pos
        for op, text in ops:
            if op == " ":
                result.append(original[cursor])
                cursor += 1
            elif op == "-":
                cursor += 1
            else:
                result.append(text)
        src_idx = cursor
    result.extend(original[src_idx:])
    return "\n".join(result)
