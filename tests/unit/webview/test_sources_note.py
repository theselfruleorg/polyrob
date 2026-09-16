"""043 R11 — every new screen names what it read, and every name resolves.

The mockup set carries a ``sources`` note on every screen for one reason: a
number nobody can trace is a number nobody can trust. The 2026-09-13 validation
pass checked all 37 identifiers cited across the 25 mockups against the tree and
found every one of them real — *no design on sand*. This test is the same bar,
moved from the drawing to the product.

Two halves:

1. **Every template that extends ``shell.html`` carries exactly one
   ``<p class="sources">``.** Zero means the screen makes claims it does not
   source; two means the note has started to drift into the body.
2. **Every identifier the note cites resolves in the tree.** A citation is a
   bold run (``<b>GoalBoard.asks</b>``) or a call form (``read_book(``) that
   looks like an identifier. Bold prose is not a citation and is not grepped —
   the mockups bold a definition as well as a name, and a rule that could not
   tell them apart would be a rule people delete.

``layout.html`` and everything built on it is exempt: the 26 legacy templates
predate both the note and the copy layer, and retrofitting them is its own task.
"""
import re
import subprocess
from pathlib import Path

import pytest

from webview.copy import STRINGS

_REPO = Path(__file__).resolve().parents[3]
_TEMPLATES = _REPO / "webview" / "templates"

_SOURCES_P = re.compile(r'<p\s+class="sources"', re.I)
#: A citation: bold text, or a name immediately followed by "(".
_BOLD = re.compile(r"<b>(.*?)</b>", re.S)
_CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_.]*)\s*\(")
#: What counts as an IDENTIFIER rather than an emphasised phrase: no spaces, and
#: it carries a ``_`` or a ``.`` or a capital, i.e. it is shaped like code.
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


def _shell_templates():
    out = []
    for path in sorted(_TEMPLATES.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        if 'extends "shell.html"' in text:
            out.append(path)
    return out


def _resolved_text(template: Path) -> str:
    """The note's text with every ``t('key')`` replaced by its string.

    The note lives half in the template and half in the copy layer — that is
    the point of the copy layer — so a checker that reads only the template
    would see three spaces where the citations are.
    """
    text = template.read_text(encoding="utf-8")

    def sub(match):
        key = match.group(1)
        return STRINGS.get(key, "")

    return re.sub(r"""\bt\(\s*['"]([^'"]+)['"]\s*\)""", sub, text)


def _note_fragment(template: Path) -> str:
    """Everything from the sources note to the end of its paragraph."""
    text = _resolved_text(template)
    match = _SOURCES_P.search(text)
    if not match:
        return ""
    end = text.find("</p>", match.end())
    return text[match.end():end if end != -1 else len(text)]


def _citations(fragment: str):
    names = set()
    for bold in _BOLD.findall(fragment):
        candidate = bold.strip()
        if _IDENT.match(candidate) and ("_" in candidate or "." in candidate
                                        or any(c.isupper() for c in candidate)):
            names.add(candidate)
    for call in _CALL.findall(fragment):
        names.add(call)
    return sorted(names)


def _grep(pattern: str) -> bool:
    """``grep -rlE --include=*.py`` over the repo, the way the 040 validation
    checked the mockups' own citations."""
    proc = subprocess.run(
        ["grep", "-rlE", "--include=*.py", "--exclude-dir=tests",
         "--exclude-dir=build", "--exclude-dir=node_modules", pattern,
         str(_REPO)],
        capture_output=True, text=True)
    # ⚠️ ``tests`` is excluded deliberately: without it this very file's own
    # negative example would resolve against itself, and a sources note may
    # only cite the product, never its tests.
    return bool(proc.stdout.splitlines())


def resolves(name: str) -> bool:
    """Is *name* a real thing in this tree?

    The full dotted form first (``core.self_evolution``), then its last segment
    as a word (``GoalBoard.asks`` is defined as ``def asks`` on a class, so the
    dotted form never appears literally). Anchored on a word boundary so
    ``askz`` cannot pass because ``asks`` exists.
    """
    if _grep(re.escape(name)):
        return True
    last = name.rsplit(".", 1)[-1]
    return _grep(r"\b" + re.escape(last) + r"\b")


# --- half one: every screen carries exactly one note ------------------------ #

@pytest.mark.parametrize("path", _shell_templates(), ids=lambda p: p.name)
def test_every_shell_screen_carries_one_sources_note(path):
    found = len(_SOURCES_P.findall(path.read_text(encoding="utf-8")))
    assert found == 1, (
        f"{path.name} has {found} sources notes; a screen names what it read "
        "exactly once (043 R11)")


def test_the_scan_found_the_new_screens():
    """A ratchet over an empty set is a ratchet nobody has seen work."""
    names = {p.name for p in _shell_templates()}
    assert names >= {"inbox.html", "work.html"}, names


def test_the_legacy_templates_are_exempt():
    """``layout.html`` and its 26 children predate the note. Retrofitting them
    is its own task; this test must not silently claim it was done."""
    legacy = [p for p in sorted(_TEMPLATES.glob("*.html"))
              if 'extends "layout.html"' in p.read_text(encoding="utf-8")]
    assert legacy, "no layout.html children found — the exemption is untested"
    assert not ({p.name for p in legacy} & {p.name for p in _shell_templates()})


# --- half two: every cited identifier is real ------------------------------- #

def _all_citations():
    out = []
    for path in _shell_templates():
        for name in _citations(_note_fragment(path)):
            out.append((path.name, name))
    return out


@pytest.mark.parametrize("template,name", _all_citations(),
                         ids=lambda v: str(v))
def test_every_cited_identifier_resolves_in_the_tree(template, name):
    assert resolves(name), (
        f"{template}'s sources note cites {name!r}, which does not exist in "
        "this tree. A note that names a thing that is gone is worse than no "
        "note: it is a claim nobody can check but everybody believes.")


def test_the_citation_check_is_not_vacuous():
    """Fed a real name and an invented one, the checker must disagree."""
    assert resolves("build_recap")
    assert resolves("GoalBoard.asks")
    assert not resolves("there_is_no_such_symbol_anywhere_qqq")


def test_the_extractor_separates_a_name_from_emphasis():
    fragment = ("An Inbox item is a <b>durable record, blocked on an owner "
                "decision</b>, read by <b>core.self_evolution</b> and "
                "list_pending_tool_approvals(.")
    assert _citations(fragment) == ["core.self_evolution",
                                    "list_pending_tool_approvals"]
