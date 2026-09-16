"""Release-artifact checks for generated development files.

Setuptools discovers namespace packages by directory name. Without explicit
exclusions, a local ``webview/dev/node_modules`` tree can therefore become part
of a wheel even though it contains no Python package markers.
"""
from __future__ import annotations

import os
from fnmatch import fnmatchcase
from pathlib import Path
import subprocess
import sys
import tomllib
import zipfile

import pytest


REPO = Path(__file__).resolve().parents[2]


def _package_config() -> dict:
    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))[
        "tool"
    ]["setuptools"]


def test_package_discovery_excludes_generated_dependency_and_bytecode_trees():
    find = _package_config()["packages"]["find"]
    candidates = []
    for directory in REPO.rglob("*"):
        if not directory.is_dir():
            continue
        relative = directory.relative_to(REPO)
        if any(part.startswith(".") for part in relative.parts):
            continue
        package = ".".join(relative.parts)
        if any(fnmatchcase(package, pattern) for pattern in find["include"]):
            if not any(fnmatchcase(package, pattern) for pattern in find["exclude"]):
                candidates.append(package)
    leaked = [
        package
        for package in candidates
        if "node_modules" in package.split(".") or "__pycache__" in package.split(".")
    ]
    assert not leaked, f"generated directories discovered as wheel packages: {leaked}"


@pytest.mark.skipif(
    os.getenv("POLYROB_TEST_BUILD_WHEEL") != "1",
    reason="set POLYROB_TEST_BUILD_WHEEL=1 for the release-artifact build",
)
def test_built_wheel_contains_no_generated_dependency_or_bytecode_files(tmp_path):
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(tmp_path)],
        cwd=REPO,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    wheel = next(tmp_path.glob("polyrob-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
    leaked = [
        name
        for name in names
        if "/node_modules/" in f"/{name}" or "/__pycache__/" in f"/{name}"
        or name.endswith((".pyc", ".pyo"))
    ]
    assert not leaked, f"generated development files shipped in wheel: {leaked[:20]}"
