"""067 (one install, owner decision 2026-09-25): the first-party packs ship INSIDE
the ``polyrob`` distribution. Their former separate distribution names were never
published — a package index may hand them to anyone — so they live in exactly ONE
place, ``core.packs.index.RETIRED_DISTS``, and every other live file imports it.

This ratchet fails when a tracked live file (history docs excepted) spells one of
those names, or points at the retired ``packs/<dist-name>/`` directory layout.
The names are built from the constant, so this file holds none of them either.
"""
import re
import subprocess
from pathlib import Path

import pytest

from core.packs.index import RETIRED_DISTS

REPO = Path(__file__).resolve().parents[1]

#: The one file that may spell the names (THE constant).
_OWNER = "core/packs/index.py"
#: Dated history (reviews, proposals, handoffs, plans, ops logs) is not rewritten.
_HISTORY = tuple(f"docs/{d}/" for d in ("reviews", "proposals", "handoffs", "plans", "ops"))


def _tracked():
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True,
                             check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [p for p in out.decode().split("\0") if p]


def _pattern():
    # A name must not continue as another identifier (polyrob-x402 is a unit name).
    names = "|".join(re.escape(n) for n in sorted(RETIRED_DISTS, key=len, reverse=True))
    return re.compile(rf"(?<![A-Za-z0-9_-])({names})(?![A-Za-z0-9_])")


def test_the_constant_holds_the_three_retired_names():
    assert len(RETIRED_DISTS) == 3
    assert all(n.startswith("polyrob-") and n == n.lower() for n in RETIRED_DISTS)


def test_no_live_file_spells_a_retired_pack_distribution_name():
    pattern = _pattern()
    old_dirs = re.compile(r"packs/(" + "|".join(re.escape(n) for n in RETIRED_DISTS) + r")\b")
    hits = []
    for rel in _tracked():
        if rel == _OWNER or rel.startswith(_HISTORY):
            continue
        path = REPO / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if pattern.search(line) or old_dirs.search(line):
                hits.append(f"{rel}:{i}: {line.strip()[:120]}")
    assert not hits, ("a retired separate pack distribution name (or its packs/<dist>/ "
                      "path) is back; import core.packs.index.RETIRED_DISTS instead:\n"
                      + "\n".join(hits))


def test_the_owner_spells_each_name_once():
    text = (REPO / _OWNER).read_text(encoding="utf-8")
    for name in RETIRED_DISTS:
        assert text.count(f'"{name}"') == 1, name


def test_no_pack_directory_is_named_like_a_distribution():
    dirs = {p.name for p in (REPO / "packs").iterdir() if p.is_dir()}
    assert not {d for d in dirs if d.lower() in RETIRED_DISTS or d.startswith("polyrob-")}, dirs
