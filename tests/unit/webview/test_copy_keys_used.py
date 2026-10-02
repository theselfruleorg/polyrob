"""070 E.4 — every console copy key is read somewhere.

A key nobody renders is a sentence nobody reviews: it keeps an old word alive in
the glossary counts and tells a reader a screen exists that does not. 070 E.4
deleted the dead ones; this keeps it that way.

A key is USED when it appears as a quoted literal in a ``webview/**/*.py``
module (other than the copy file itself) or a ``webview/templates/*.html``
template, or when it belongs to one of the run-time key families below. Each
family is a narrow prefix with a named reader, never a whole destination.
"""
import re
from pathlib import Path

from webview.copy import STRINGS

_WEBVIEW = Path(__file__).resolve().parents[3] / "webview"
_COPY_FILE = _WEBVIEW / "copy" / "en.py"


def _dynamic_families() -> dict:
    """``{prefix: reader}`` for the keys built at run time."""
    from core.surfaces.inbox import ACTIONS
    return {
        # pages_new._card: f"inbox.action.{action}" for each core action.
        **{f"inbox.action.{a}": "pages_new._card" for a in ACTIONS},
        "inbox.age.": "pages_new._age_words",
        "agent.axis_value.": "pages_new.axis_answer",
        "agent.memory_backend.": "agent.html (memory_backend_<id>)",
        "chat.run_head.": "chat_open.first_line_context (run_head.<creator>, 070 W0.12)",
        # pages_new: t(f"{current}.title") for the five destinations.
        **{f"{d}.title": "pages_new page title" for d in ("new", "inbox", "work", "money", "agent")},
    }


def _corpus() -> str:
    parts = []
    for path in sorted(_WEBVIEW.rglob("*.py")):
        if path == _COPY_FILE or "node_modules" in path.parts:
            continue
        parts.append(path.read_text(encoding="utf-8"))
    for path in sorted((_WEBVIEW / "templates").glob("*.html")):
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def _unused() -> list:
    corpus = _corpus()
    literals = set(re.findall(r"""['"]([a-z0-9_]+(?:\.[a-z0-9_]+)+)['"]""", corpus))
    families = _dynamic_families()
    out = []
    for key in sorted(STRINGS):
        if key in literals:
            continue
        if any(key == p or (p.endswith(".") and key.startswith(p)) for p in families):
            continue
        out.append(key)
    return out


def test_every_key_is_used():
    unused = _unused()
    assert not unused, (
        "console copy keys that nothing renders — delete them from "
        f"webview/copy/en.py or wire them: {unused}")


def test_the_allowlist_is_not_a_blanket():
    destinations = {"new", "inbox", "work", "money", "agent", "chat", "chats", "shell"}
    for prefix in _dynamic_families():
        assert any(k == prefix or k.startswith(prefix) for k in STRINGS), (
            f"family {prefix!r} matches no key — drop it")
        assert prefix.rstrip(".") not in destinations, (
            f"family {prefix!r} is a whole destination")
        assert prefix.count(".") >= 1


def test_the_scan_sees_a_used_and_an_unused_key():
    corpus = _corpus()
    assert "inbox.checked_all" in corpus
    assert "nothing.like.this" not in corpus
