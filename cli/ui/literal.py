"""Render external text literally; terminal escapes are never content formatting."""
import re

_CONTROL = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\|$)"
    r"|\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b[@-_]"
    r"|[\x00-\x08\x0b-\x1f\x7f-\x9f]"
)


def literal_text(text):
    return _CONTROL.sub("", str(text))
