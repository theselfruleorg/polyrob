"""066 P1 — the shipped closures and requirements.txt equal what the lock says.

The same both-generators discipline as the flag catalog: `requirements.lock`
changes -> `python scripts/gen_lazy_closures.py` in the SAME commit, or this fails.
Regenerates in memory from ``core.lock_closure`` (no ``scripts/`` import: that
tree does not ship in the public package).
"""
import re
from pathlib import Path

import pytest

from core import lock_closure as lc
from core.lazy_closures import closure_dir, read_closure, shipped_lock_digest
from core.lazy_deps import LAZY_DEPS, _dist_name

ROOT = Path(__file__).resolve().parents[3]
REGEN = "python scripts/gen_lazy_closures.py"


@pytest.fixture(scope="module")
def lock_text():
    return (ROOT / "requirements.lock").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def lock(lock_text):
    return lc.parse_lock(lock_text)


def test_the_lock_is_hashed(lock_text, lock):
    assert "--generate-hashes" in "\n".join(lock_text.splitlines()[:3])
    unhashed = [n for n, entries in lock.entries.items() for e in entries if not e.hashes]
    assert unhashed == [], f"pins without a hash: {unhashed[:5]}"


def test_every_lazy_row_has_a_closure_and_nothing_else_does():
    """067 (one install): the first-party packs ship inside polyrob, so they have
    no install closure of their own (their SDKs are core extras)."""
    shipped = {p.stem for p in closure_dir().glob("*.txt")}
    assert shipped == set(LAZY_DEPS), f"rerun `{REGEN}`"


def test_the_lock_compiles_the_core_pyproject_with_the_one_command(lock_text, lock):
    header = lock_text.splitlines()[1]
    assert header == "#    " + " ".join(lc.lock_command(ROOT)), (
        f"requirements.lock was not compiled by the current command; run "
        f"`python scripts/gen_lazy_closures.py --relock`")
    assert "polyrob" not in lock.entries and "pandas" not in lock.entries


@pytest.mark.parametrize("feature", sorted(LAZY_DEPS))
def test_each_closure_equals_the_lock(feature, lock):
    expected = lc.render_feature(lock, feature, [_dist_name(s) for s in LAZY_DEPS[feature]])
    actual = (closure_dir() / f"{feature}.txt").read_text(encoding="utf-8")
    assert actual == expected, f"core/lazy_closures/{feature}.txt drifted from requirements.lock; rerun `{REGEN}`"
    c = read_closure(feature)
    assert c.lock_digest == lock.digest
    names = {_dist_name(s).replace("_", "-") for s in LAZY_DEPS[feature]}
    assert names <= {n for n, _ in c.pins}, "a closure must contain its own row"


def test_every_lazy_closure_entry_is_verbatim_in_the_lock(lock):
    blocks = {e.block for entries in lock.entries.values() for e in entries}
    for feature in LAZY_DEPS:
        for _, _, block in read_closure(feature).blocks():
            assert block in blocks, f"{feature}: {block.splitlines()[0]} is not the lock's entry"


def test_all_closures_share_the_lock_digest(lock):
    assert shipped_lock_digest() == lock.digest


def test_requirements_txt_equals_the_closure_of_its_extras(lock_text):
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    m = lc.REQUIREMENTS_TXT_EXTRAS_RE.search(text)
    assert m, "requirements.txt lost its `# extras:` line"
    expected = lc.render_requirements_txt(lock_text, ROOT / "pyproject.toml", m.group(1).split(","))
    assert text == expected, f"requirements.txt drifted from requirements.lock; rerun `{REGEN}`"


def test_the_closures_ship_as_package_data():
    """setuptools' ``"*" = ["*.txt"]`` glob carries them in the wheel — the lock
    itself ships only in the sdist, so a pipx install needs these."""
    import tomllib
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "*.txt" in data["tool"]["setuptools"]["package-data"]["*"]
    assert (closure_dir() / "__init__.py").is_file(), "core.lazy_closures must be a package"
    include = data["tool"]["setuptools"]["packages"]["find"]["include"]
    assert any(re.fullmatch(p.replace("*", ".*"), "core.lazy_closures") for p in include)


def test_the_closure_reader_is_stdlib_only_and_runs_as_a_script():
    """install.sh / deploy_prod.sh run core/lock_closure.py BEFORE the project is
    installed: it may import nothing from the tree."""
    src = (ROOT / "core" / "lock_closure.py").read_text()
    imports = re.findall(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", src, re.M)
    assert not [i for i in imports if i.split(".")[0] in {"core", "agents", "modules", "tools", "cli"}]
    import subprocess
    import sys
    out = subprocess.run([sys.executable, str(ROOT / "core" / "lock_closure.py"), "digest",
                          "--lock", str(ROOT / "requirements.lock")], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == lc.lock_digest((ROOT / "requirements.lock").read_text(encoding="utf-8"))


def test_the_prod_closure_matches_uv_shape(lock):
    """The via-graph closure of the server set includes every root and the
    build backend, and leaves out the stacks prod does not declare."""
    import tomllib
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    extras = "server,browser,crypto,solana,telegram,voice,docs,media".split(",")
    names = lc.closure(lock, lc.project_roots(project, extras))
    assert {"setuptools", "fastapi", "playwright", "web3", "aiogram", "faster-whisper"} <= names
    assert not {"torch", "sentence-transformers", "anthropic", "google-generativeai"} & names
