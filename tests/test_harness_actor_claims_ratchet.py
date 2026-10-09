"""Harness text must not assert that the user/owner did something it did not record.

Prod 2026-10-03: a snapshot-diff note said "The user just uploaded new files"
about a file the maintenance loop wrote; the agent told the owner "the file you
uploaded" and then "it was never there". This ratchet scans every STRING literal
(comments are free) in the agent harness for an unrecorded owner-action claim.
A real, recorded claim goes in ALLOWED with the reason.
"""
import io
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("agents", "core", "tools", "surfaces", "api", "cli", "cron", "modules")

BANNED = re.compile(
    r"\b(?:the\s+)?(?:user|owner)\s+(?:has\s+|just\s+)*"
    r"(?:uploaded|dropped|added|sent\s+you)\s+(?:a\s+|new\s+|the\s+)?files?\b"
    r"|\byou\s+uploaded\b"
    r"|\bnew\s+files?\s+uploaded\b",
    re.IGNORECASE,
)

# path -> reason. Keep empty unless the claim is backed by an upload record.
ALLOWED: dict = {}


def _string_literals(path: Path):
    try:
        toks = tokenize.generate_tokens(io.StringIO(path.read_text(encoding="utf-8")).readline)
        for tok in toks:
            if tok.type == tokenize.STRING or tok.type == getattr(tokenize, "FSTRING_MIDDLE", -1):
                yield tok.start[0], tok.string
    except (tokenize.TokenError, SyntaxError, UnicodeDecodeError):
        return


def test_no_unrecorded_owner_action_claims_in_harness_strings():
    hits = []
    for d in SCAN_DIRS:
        for path in (ROOT / d).rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if "/tests/" in f"/{rel}" or rel in ALLOWED:
                continue
            for line, s in _string_literals(path):
                if BANNED.search(s):
                    hits.append(f"{rel}:{line}: {s[:100]}")
    assert not hits, (
        "Harness text claims the user/owner did something the harness did not record "
        "(see the module docstring):\n" + "\n".join(hits)
    )


def test_ratchet_catches_the_2026_10_03_wording():
    for s in ("The user just uploaded new files", "NEW FILES UPLOADED",
              "the changelog file you uploaded", "owner has added a file"):
        assert BANNED.search(s), s
    assert not BANNED.search("uploaded by the owner via the console")
