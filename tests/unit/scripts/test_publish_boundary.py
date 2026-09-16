"""Every tracked file must be EXPLICITLY classified for the public export.

The private->public boundary is two lists: `scripts/private_paths.txt` (deny,
fail-closed) and `scripts/public_manifest.txt` (allow). A file matching neither
is private only by ACCIDENT — it stays out because no allow row happens to
match, not because a rule says so. That is exactly how `docs/architecture/`
nearly shipped at 0.13.0: an UNANCHORED `README.md` allow row matched
`docs/architecture/README.md`, and the only thing that would have caught it was
a scrub-gate check on its content.

So: unclassified is a defect, and an allow row must never be broad enough to
reach inside a denied tree.
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
DENY_FILE = ROOT / "scripts" / "private_paths.txt"
ALLOW_FILE = ROOT / "scripts" / "public_manifest.txt"

pytestmark = pytest.mark.skipif(
    not (DENY_FILE.is_file() and ALLOW_FILE.is_file()),
    reason="scripts/ is operator tooling, absent from the public tree",
)


def _rows(path: Path) -> list[str]:
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def _public_owned() -> set[str]:
    """Paths public maintains its OWN copy of — deliberately never exported."""
    return {ln.split(":", 1)[1].strip()
            for ln in ALLOW_FILE.read_text(encoding="utf-8").splitlines()
            if ln.strip().startswith("#public-owned:")}


def _match(rows: list[str], path: str) -> bool:
    return any(re.search(r, path) for r in rows)


def _tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                         text=True, check=True).stdout
    return [p for p in out.splitlines() if p]


def test_every_tracked_file_is_classified():
    deny, allow = _rows(DENY_FILE), _rows(ALLOW_FILE)
    owned = _public_owned()
    unclassified = [p for p in _tracked_files()
                    if p not in owned
                    and not _match(deny, p) and not _match(allow, p)]
    assert not unclassified, (
        "these files match NEITHER list, so they are private only by accident — "
        "add each to scripts/private_paths.txt (or the manifest if it should "
        f"ship): {sorted(unclassified)[:20]}"
    )


def test_no_bare_filename_allow_row_reaches_into_a_denied_tree():
    """A DIRECTORY allow row overlapping the denylist is the intended pattern —
    `tests/` ships broadly and the denylist carves out individual lab tests.

    A bare FILENAME row is different: it has no directory to scope it, so it
    matches that name anywhere in the repo. That is the `README.md` ->
    `docs/architecture/README.md` bug, and there the denylist is the only thing
    standing between a private file and the public repo.
    """
    deny = _rows(DENY_FILE)
    bare = [r for r in _rows(ALLOW_FILE) if "/" not in r]
    overlap = [(r, p) for p in _tracked_files() if _match(deny, p)
               for r in bare if re.search(r, p)]
    assert not overlap, (
        "these bare-filename allow rows reach into DENIED paths; anchor them "
        f"`^name$`: {sorted(set(overlap))[:20]}"
    )


def test_root_level_manifest_rows_are_anchored():
    """`README.md` as a bare row is a substring regex: it matches every nested
    README.md in the repo, including ones inside private trees."""
    unanchored = [r for r in _rows(ALLOW_FILE)
                  if "/" not in r and not r.startswith("^")]
    assert not unanchored, (
        "root-level manifest rows must be anchored `^name$` or they match "
        f"nested files of the same name: {unanchored}"
    )
