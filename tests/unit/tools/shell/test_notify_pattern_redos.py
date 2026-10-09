"""A notify_pattern runs on the session's event loop with no time bound, so the
catastrophic-backtracking shapes are refused at compile time."""
import pytest

from tools.shell.watch import compile_pattern


@pytest.mark.parametrize("raw", ["(a+)+", "((a+))+", r"(\w*)*", "(x{1,9}){2,}",
                                 r"(.*a){12}", r"(a)\1", r"(?P<x>a)(?P=x)"])
def test_refuses_catastrophic_shapes(raw):
    with pytest.raises(ValueError):
        compile_pattern(raw)


@pytest.mark.parametrize("raw", ["ERROR|FAIL", "(done|ready)", "(a|b)+", r"\(a+\)+",
                                 "[(a+)]+", "(?:ab+)c", "(a+)?", r"listening on :\d+"])
def test_accepts_ordinary_patterns(raw):
    assert compile_pattern(raw).pattern == raw
