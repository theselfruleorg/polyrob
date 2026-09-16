"""The curated characters must ship in the wheel AND the sdist (F12).

The published 0.13.0 wheel contained exactly ONE character
(``agents/personality/characters/polyrob.character.json``) because of two
independent one-line omissions:

* ``[tool.setuptools.package-data]`` enumerated ``prompts`` and ``streams``
  under ``"data"`` and omitted ``characters``. The ``"*" = ["*.json", ...]``
  glob above it is NOT recursive, so it catches ``data/subscription_channels.json``
  (package root) and misses ``data/characters/*.json`` (one directory down).
* ``MANIFEST.in`` grafted ``data/prompts`` and ``data/streams`` but not
  ``data/characters``, so the sdist missed them too.

Tier 3 of ``persona_resolver.character_search_dirs()`` was therefore permanently
EMPTY on every pip install: ``/persona`` listed one row and the framework's whole
"pick a personality" offering never reached a PyPI user.

Same class as "the 0.10.0 wheel omitted ``migrations/``" (see
``test_migrations_packaging.py``), one release later. These tests read the
declarations as data — offline and cheap. The real wheel build is opt-in via
``POLYROB_TEST_BUILD_WHEEL=1``.
"""

import fnmatch
import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CHARACTERS_DIR = REPO_ROOT / "data" / "characters"


def _repo_character_files() -> list[str]:
    """Every curated character the repo ships, as ``data``-relative posix paths."""
    return sorted(
        p.relative_to(REPO_ROOT / "data").as_posix()
        for p in CHARACTERS_DIR.glob("*.character.json")
    )


def _data_package_globs() -> list[str]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        pyproject = tomllib.load(fh)
    return list(pyproject["tool"]["setuptools"]["package-data"].get("data", []))


def test_repo_actually_ships_curated_characters():
    """Guard the guard: the rest of this module is vacuous with no characters."""
    assert _repo_character_files(), (
        f"no *.character.json in {CHARACTERS_DIR} — the curated set is the thing "
        "these packaging contracts exist to protect"
    )


def test_every_repo_character_matches_a_package_data_glob():
    """Adding a preset without shipping it must fail here, not on PyPI."""
    globs = _data_package_globs()
    unshipped = [
        rel for rel in _repo_character_files()
        if not any(fnmatch.fnmatch(rel, g) for g in globs)
    ]
    assert not unshipped, (
        f"{unshipped} match no [tool.setuptools.package-data] \"data\" glob "
        f"({globs}) — they will be absent from the wheel. Note the \"*\" glob is "
        "NOT recursive, so a subdirectory needs its own entry."
    )


def test_manifest_grafts_data_characters():
    """The sdist path is a second, independent omission — pin it separately."""
    manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    grafted = {
        line.split(None, 1)[1].strip().rstrip("/")
        for line in manifest.splitlines()
        if line.strip().startswith("graft ")
    }
    assert "data/characters" in grafted, (
        "MANIFEST.in must `graft data/characters` or the sdist omits every "
        f"curated character (grafts today: {sorted(grafted)})"
    )


def test_character_search_dirs_reaches_the_install_tree_characters():
    """Tier 3 must resolve to the CODE tree's data/characters, never the cwd."""
    from agents.personality.persona_resolver import character_search_dirs

    dirs = character_search_dirs()
    install_tier = Path(
        __import__("agents.personality.persona_resolver", fromlist=["x"]).__file__
    ).resolve().parents[2] / "data" / "characters"
    assert install_tier in [d.resolve() for d in dirs], (
        f"install-tree tier {install_tier} missing from {dirs}"
    )


@pytest.mark.skipif(
    os.environ.get("POLYROB_TEST_BUILD_WHEEL", "").strip().lower()
    not in {"1", "true", "yes"},
    reason="set POLYROB_TEST_BUILD_WHEEL=1 to build a real wheel (slow)",
)
def test_built_wheel_contains_every_curated_character(tmp_path):
    """The clean-room contract: a real wheel exposes the repo's whole set."""
    proc = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(tmp_path)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=900,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    wheels = sorted(tmp_path.glob("*.whl"))
    assert wheels, "no wheel produced"
    with zipfile.ZipFile(wheels[-1]) as zf:
        shipped = sorted(
            n[len("data/"):] for n in zf.namelist()
            if n.startswith("data/characters/") and n.endswith(".character.json")
        )
    assert shipped == _repo_character_files()
