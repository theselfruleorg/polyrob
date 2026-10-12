"""docs/CONFIGURATION.md names no private work-item ids.

The doc ships in the sdist and feeds the user-guide reference the agent reads.
A row tagged "057 WS-H:" or "066 P2:" tells an operator nothing — the ids point
into private proposals and handoffs. Say what the flag does; the history lives
in git and CHANGELOG.md. Zero tolerance: the existing tags were stripped on
2026-10-10.

Skipped when the doc is absent (an installed wheel ships no docs/).
"""
import pathlib
import re

import pytest

DOC = pathlib.Path(__file__).resolve().parents[1] / "docs" / "CONFIGURATION.md"

_SUB = r"(?:WS|WP|W|P|D|F|A|R|L|B|C|E|T|S|lanes?|phase|§)-?\d"
PATTERNS = {
    "proposal-number + stream (`057 WS-H`, `066 P2`)": re.compile(rf"\b0\d\d {_SUB}"),
    "`pre-0NN` / `pre-F8`": re.compile(r"\bpre-(?:0\d\d|F\d+)\b"),
    "`proposal 0NN`": re.compile(r"\bproposal \d{3}\b"),
    "work stream id (`WS-K4`)": re.compile(r"\bWS-?[A-Z]?\d+\b"),
    "leading cell tag (`| F7:`, `| 061:`)": re.compile(
        r"\| (?:\*\*)?(?:0\d\d|[A-Z]{1,4}-?\d+[a-z]?(?:\([ivx]+\))?)(?:\*\*)?"
        r"(?: \([^)]*\))?:"),
}


@pytest.mark.skipif(not DOC.exists(), reason="docs/ not shipped in this install")
def test_configuration_doc_has_no_work_item_ids():
    hits = []
    for lineno, line in enumerate(DOC.read_text().splitlines(), 1):
        for label, rx in PATTERNS.items():
            m = rx.search(line)
            if m:
                hits.append(f"line {lineno}: {label}: ...{line[max(0, m.start()-30):m.end()+20]}...")
    assert not hits, (
        "docs/CONFIGURATION.md names private work-item ids — describe the "
        "behaviour instead (history belongs in git/CHANGELOG):\n" + "\n".join(hits[:40])
    )
