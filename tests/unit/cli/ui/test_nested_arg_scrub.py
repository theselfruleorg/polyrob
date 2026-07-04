"""Regression: nested (dict/list) tool-call arg values must be secret-scrubbed.

Only top-level STRING args were scrubbed; a credential nested inside a dict/list
arg (headers={'Authorization': 'Bearer sk-...'}) reached the default terminal
transcript unredacted via str(value).
"""
from rich.text import Text

from cli.ui.blocks import _append_args, _format_value

SECRET = "Bearer sk-live-ABCDEF0123456789abcdef"


def test_append_args_scrubs_nested_dict():
    line = Text()
    _append_args(line, {"headers": {"Authorization": SECRET}}, "")
    assert "sk-live-ABCDEF0123456789abcdef" not in line.plain
    assert "redacted" in line.plain.lower()


def test_append_args_scrubs_nested_list():
    line = Text()
    _append_args(line, {"auth": [SECRET]}, "")
    assert "sk-live-ABCDEF0123456789abcdef" not in line.plain


def test_format_value_scrubs_non_str():
    out = _format_value({"api_key": "sk-ABCDEF0123456789abcdef0123"})
    assert "sk-ABCDEF0123456789abcdef0123" not in out


def test_append_args_scrubs_args_str_fallback():
    line = Text()
    # params not a dict -> args_str fallback path must scrub too.
    _append_args(line, None, f"headers={{'Authorization': '{SECRET}'}}")
    assert "sk-live-ABCDEF0123456789abcdef" not in line.plain


def test_top_level_string_still_scrubbed():
    line = Text()
    _append_args(line, {"token": SECRET}, "")
    assert "sk-live-ABCDEF0123456789abcdef" not in line.plain
