"""Slash-command input highlighting: classifier + Processor adapter."""
from cli.ui.slash_highlight import (
    CLASS_ARG,
    CLASS_PARTIAL,
    CLASS_UNKNOWN,
    CLASS_VALID,
    SlashHighlightProcessor,
    classify_slash,
)

_KNOWN = {"help", "model", "goals"}


def _is_known(w):
    return w in _KNOWN


def _is_prefix(w):
    return any(n.startswith(w) for n in _KNOWN)


def test_non_slash_text_fast_path():
    assert classify_slash("hello world", _is_known, _is_prefix) == []


def test_valid_command():
    assert classify_slash("/help", _is_known, _is_prefix) == [(0, 5, CLASS_VALID)]


def test_partial_prefix():
    assert classify_slash("/mo", _is_known, _is_prefix) == [(0, 3, CLASS_PARTIAL)]


def test_unknown_command():
    assert classify_slash("/xyzzy", _is_known, _is_prefix) == [(0, 6, CLASS_UNKNOWN)]


def test_bare_slash_is_partial():
    assert classify_slash("/", _is_known, _is_prefix) == [(0, 1, CLASS_PARTIAL)]


def test_args_get_arg_class():
    spans = classify_slash("/model glm-5.2", _is_known, _is_prefix)
    assert spans == [(0, 6, CLASS_VALID), (7, 14, CLASS_ARG)]


def test_case_insensitive_lookup():
    assert classify_slash("/HELP", _is_known, _is_prefix)[0][2] == CLASS_VALID


def _apply(proc, text, lineno=0):
    """Drive apply_transformation with a real TransformationInput-shaped stub."""
    from prompt_toolkit.document import Document

    class _TI:
        document = Document(text)
        fragments = [("", text)]
        def __init__(self):
            self.lineno = lineno
    return proc.apply_transformation(_TI())


def test_processor_highlights_valid_command():
    proc = SlashHighlightProcessor(is_known=_is_known, is_prefix=_is_prefix)
    result = _apply(proc, "/help")
    styles = "".join(frag[0] for frag in result.fragments)
    assert CLASS_VALID in styles


def test_processor_passthrough_non_slash():
    proc = SlashHighlightProcessor(is_known=_is_known, is_prefix=_is_prefix)
    result = _apply(proc, "just chatting")
    assert result.fragments == [("", "just chatting")]


def test_processor_gated_off():
    proc = SlashHighlightProcessor(gate=lambda: True, is_known=_is_known, is_prefix=_is_prefix)
    result = _apply(proc, "/help")
    assert result.fragments == [("", "/help")]


def test_processor_never_raises_on_bad_binding():
    proc = SlashHighlightProcessor(is_known=None, is_prefix=None)
    proc._bind = lambda: False  # simulate registry import failure
    result = _apply(proc, "/help")
    assert result.fragments == [("", "/help")]
