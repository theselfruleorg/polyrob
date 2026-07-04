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
