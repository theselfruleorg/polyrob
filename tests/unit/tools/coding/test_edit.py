"""C1 — str_replace pure-logic tests (exact, unique-or-fail, replace_all)."""
import pytest

from tools.coding.edit import apply_str_replace, EditError


def test_replaces_unique_occurrence():
    out = apply_str_replace("hello world", "world", "there")
    assert out == "hello there"


def test_zero_match_fails_loudly():
    with pytest.raises(EditError, match="not found"):
        apply_str_replace("hello world", "absent", "x")


def test_ambiguous_match_without_replace_all_fails():
    with pytest.raises(EditError, match="not unique"):
        apply_str_replace("a a a", "a", "b")


def test_replace_all_replaces_every_occurrence():
    out = apply_str_replace("a a a", "a", "b", replace_all=True)
    assert out == "b b b"


def test_identical_old_new_fails():
    with pytest.raises(EditError, match="identical"):
        apply_str_replace("x", "same", "same")


def test_unique_match_preserves_surrounding_text_exactly():
    src = "def f():\n    return 1\n\ndef g():\n    return 2\n"
    out = apply_str_replace(src, "    return 1", "    return 42")
    assert out == "def f():\n    return 42\n\ndef g():\n    return 2\n"


# ---------------------------------------------------------------------------
# I-8 — whitespace-tolerant fallback (still unique-or-fail)
# ---------------------------------------------------------------------------
# NOTE: these two cases are the brief's literal Step 5.1 examples, kept for
# spec traceability. Neither actually forces the new fallback ladder to run:
# `"    return 1"` (4-space) is already an exact substring of
# `"        return 1"` (8-space) at a shifted offset (repeated-space
# coincidence), and the ambiguous case resolves via the pre-existing exact
# `count > 1` branch (content.count("x = 1") == 2) before any fallback code
# is reached. The tests below force genuine 0-exact-match situations so the
# new code path has real RED -> GREEN coverage.


def test_str_replace_matches_across_indentation_drift():
    content = "def f():\n        return 1\n"          # 8-space indent
    out = apply_str_replace(content, "    return 1", "    return 2")  # 4-space in old_string
    assert out == "def f():\n        return 2\n"


def test_str_replace_still_fails_when_ambiguous_after_normalization():
    content = "x = 1\nx = 1\n"
    with pytest.raises(EditError):
        apply_str_replace(content, "x = 1", "x = 2")   # 2 matches, no replace_all -> loud fail


def test_str_replace_matches_when_old_string_over_indented():
    # content is genuinely LESS indented than old_string -> old_string is NOT
    # an exact substring anywhere (count == 0), forcing the normalized
    # fallback. Exercises the "shrink" (negative delta) re-indent branch.
    content = "def f():\n    return 1\n"              # 4-space indent
    out = apply_str_replace(content, "        return 1", "        return 2")  # 8-space
    assert out == "def f():\n    return 2\n"


def test_str_replace_matches_across_trailing_whitespace_drift():
    # old_string carries trailing whitespace the file line doesn't have ->
    # count == 0 exactly, forcing the fallback via line-wise .strip() compare.
    content = "x = 1\n"
    out = apply_str_replace(content, "x = 1   ", "x = 2")
    assert out == "x = 2\n"


def test_str_replace_normalized_fallback_still_ambiguous_fails_loudly():
    # Two tab-indented lines are identical after stripping, and old_string's
    # space-indent means count == 0 exactly (tabs != spaces, no substring
    # coincidence) -> this exercises the FALLBACK's OWN uniqueness guard,
    # not the pre-existing exact-match ambiguity branch.
    content = "def f():\n\treturn 1\ndef g():\n\treturn 1\n"
    with pytest.raises(EditError):
        apply_str_replace(content, "        return 1", "        return 2")


def test_str_replace_normalized_fallback_genuinely_absent_fails_loudly():
    content = "def f():\n\treturn 1\n"
    with pytest.raises(EditError, match="not found"):
        apply_str_replace(content, "        return 99", "        return 2")


def test_str_replace_replace_all_does_not_use_normalized_fallback():
    # Brief: "replace_all semantics stay exact-match-only" — a whitespace
    # drifted old_string that WOULD match via the fallback must still fail
    # loudly when replace_all=True (no normalization applied to that path).
    content = "def f():\n    return 1\n"
    with pytest.raises(EditError, match="not found"):
        apply_str_replace(content, "        return 1", "        return 2", replace_all=True)


def test_multiline_block_reindented_uniquely():
    # Exercises the n>1 window-scan loop in _apply_normalized_fallback
    # AND the growing (delta > 0) branch of _reindent_by_delta: old_string
    # is a genuine 2-line block, given at 4-space indent, while the real
    # block in content sits at 8-space indent (0 exact matches -> forces
    # the normalized fallback; a single window uniquely matches).
    content = (
        "def outer():\n"
        "    if True:\n"
        "        x = 1\n"
        "        y = 2\n"
        "    return x\n"
    )
    old_string = "    x = 1\n    y = 2"
    new_string = "    x = 10\n    y = 20"
    out = apply_str_replace(content, old_string, new_string)
    assert out == (
        "def outer():\n"
        "    if True:\n"
        "        x = 10\n"
        "        y = 20\n"
        "    return x\n"
    )


def test_multiline_ambiguous_after_normalization_fails():
    # The same 2-line block appears twice in content, at two different
    # indents (4-space and 8-space); old_string uses yet a third indent
    # (2-space), so it has 0 exact matches AND the normalized window scan
    # finds 2 candidate windows -> must fail loudly, never guess.
    content = (
        "if a:\n"
        "    p = 1\n"
        "    q = 2\n"
        "if b:\n"
        "        p = 1\n"
        "        q = 2\n"
    )
    old_string = "  p = 1\n  q = 2"
    with pytest.raises(EditError) as excinfo:
        apply_str_replace(content, old_string, "  p = 10\n  q = 20")
    message = str(excinfo.value)
    assert "not unique" in message
    assert "2 matches" in message


# ---------------------------------------------------------------------------
# apply_patch tests (C2 — unified-diff applier)
# ---------------------------------------------------------------------------
from tools.coding.edit import apply_patch  # noqa: E402


def test_apply_patch_applies_single_hunk():
    content = "a\nb\nc\nd\n"
    patch = "@@ -2,2 +2,3 @@\n b\n-c\n+C\n+c2\n d\n"
    assert apply_patch(content, patch) == "a\nb\nC\nc2\nd\n"


def test_apply_patch_rejects_context_mismatch():
    content = "a\nb\nc\n"
    patch = "@@ -1,2 +1,2 @@\n a\n-X\n+Y\n"   # '-X' does not match 'b'
    with pytest.raises(EditError):
        apply_patch(content, patch)


def test_apply_patch_requires_hunk_header():
    with pytest.raises(EditError):
        apply_patch("a\n", "just some text\n")


def test_apply_patch_pure_addition():
    content = "x\ny\n"
    patch = "@@ -1,2 +1,3 @@\n x\n+new\n y\n"
    assert apply_patch(content, patch) == "x\nnew\ny\n"
