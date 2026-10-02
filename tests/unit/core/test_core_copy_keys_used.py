"""Every core copy key is read somewhere — the core-tier twin of
``tests/unit/webview/test_copy_keys_used.py``.

A key nobody renders is a sentence nobody reviews. A key in
``core/copy/en.py`` is USED when it appears as a quoted literal (``'…'``,
``"…"`` or a JS backtick) in a shipped ``.py``/``.html``/``.js`` file other
than the copy file itself, or when it belongs to one of the run-time key
families below. Each family is a narrow prefix with a named reader whose
f-string prefix must appear in the corpus, never a whole screen.
"""
import re
from pathlib import Path

from core.copy.en import STRINGS

_REPO = Path(__file__).resolve().parents[3]
_COPY_FILE = _REPO / "core" / "copy" / "en.py"
_SCAN_DIRS = ("agents", "api", "cli", "core", "cron", "modules", "packs",
              "surfaces", "tools", "utils", "webview")
_SUFFIXES = (".py", ".html", ".js")


def _dynamic_families() -> dict:
    """``{prefix: reader}`` for the keys built at run time."""
    return {
        # webview/pages.py: f"status.section.{sid}" for each status-snapshot section.
        "status.section.": "webview/pages.py (f\"status.section.{sid}\")",
    }


def _corpus() -> str:
    parts = []
    for top in _SCAN_DIRS:
        root = _REPO / top
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix not in _SUFFIXES or path == _COPY_FILE:
                continue
            if "node_modules" in path.parts or "__pycache__" in path.parts:
                continue
            try:
                parts.append(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
    return "\n".join(parts)


def _unused(corpus: str) -> list:
    literals = set(re.findall(r"""['"`]([a-z0-9_]+(?:\.[a-z0-9_]+)+)['"`]""", corpus))
    families = _dynamic_families()
    return [k for k in sorted(STRINGS)
            if k not in literals
            and not any(k.startswith(p) for p in families)]


def test_every_core_key_is_used():
    unused = _unused(_corpus())
    assert not unused, (
        "core copy keys that nothing renders — delete them from "
        f"core/copy/en.py or wire them: {unused}")


def test_each_family_has_a_live_reader():
    corpus = _corpus()
    for prefix in _dynamic_families():
        assert any(k.startswith(prefix) for k in STRINGS), (
            f"family {prefix!r} matches no key — drop it")
        assert prefix.count(".") >= 2, f"family {prefix!r} is too broad"
        # The reader builds the key from this prefix in an f-string.
        assert re.search(r"""f['"]""" + re.escape(prefix) + r"\{", corpus), (
            f"no f-string builds keys from {prefix!r} — the family is stale")


def test_the_scan_sees_a_used_and_an_unused_key():
    corpus = _corpus()
    assert "chat.act.failed" in corpus
    assert "nothing.like.this" not in corpus
