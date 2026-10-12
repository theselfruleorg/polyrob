"""No test file may sit where pytest silently refuses to look.

``norecursedirs`` in pytest.ini matches a directory BASENAME at any depth. A
``scripts`` row there (meant for the repo-root live-key runners) also pruned
``tests/unit/scripts/`` — 278 tests, the publish-boundary test among them, that
CI never ran. This pins the rule: no directory under a test root may match a
norecursedirs pattern, and the root-only exclusion lives in the root
conftest.py ``collect_ignore`` (anchored to the repo root).
"""
import configparser
import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _ini():
    cfg = configparser.ConfigParser()
    cfg.read(ROOT / "pytest.ini", encoding="utf-8")
    return cfg["pytest"]


def test_no_test_file_sits_under_a_norecursedirs_name():
    patterns = _ini()["norecursedirs"].split()
    roots = [ROOT / p for p in _ini()["testpaths"].split() if (ROOT / p).is_dir()]
    hidden = []
    for root in roots:
        for f in root.rglob("test_*.py"):
            parts = f.relative_to(root).parts[:-1]
            if "__pycache__" in parts:
                continue
            if any(fnmatch.fnmatch(d, pat) for d in parts for pat in patterns):
                hidden.append(str(f.relative_to(ROOT)))
    assert not hidden, f"pytest never collects these (norecursedirs basename match): {hidden}"


def test_the_root_runner_exclusion_is_anchored_to_the_repo_root():
    patterns = _ini()["norecursedirs"].split()
    assert "scripts" not in patterns, "use the root conftest.py collect_ignore, not a basename"
    ns: dict = {}
    exec((ROOT / "conftest.py").read_text(encoding="utf-8"), ns)
    assert "scripts" in ns["collect_ignore"]


def test_tests_is_a_test_root():
    assert "tests" in _ini()["testpaths"].split()
