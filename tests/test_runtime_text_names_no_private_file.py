"""Shipped runtime text never tells a user to run a file the public tree lacks.

``scripts/`` and ``deployment/`` are operator tooling: they never ship (not in
the wheel, not in the public repo). A refusal, remedy, health line or rendered
file that says "run scripts/x.sh" or "cp deployment/y" is an instruction a
public user cannot follow. Name a CLI verb or a page under docs/guide instead.

Scope: every string literal (f-string parts included) in the shipped Python
packages, docstrings and comments excluded (they explain, they do not
instruct). ``ALLOWED`` holds the deliberate exceptions, each with its reason.
"""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: the shipped top-level code dirs (pyproject [tool.setuptools.packages.find]).
SHIPPED = ("agents", "api", "cli", "core", "polyrob", "cron", "datagen", "migrations", "modules",
           "surfaces", "tools", "utils", "webview", "packs")

_PRIVATE_PATH = re.compile(r"(?<![\w./-])(scripts|deployment)/[\w.@/-]*[\w@]")

ALLOWED = {
    ("cli/commands/init.py", "scripts/tests"): "prose ('for scripts/tests'), not a path",
    ("core/lock_closure.py", "scripts/deploy_prod.sh"):
        "provenance line in the generated requirements.txt header",
    ("surfaces/telegram/dev_rail.py", "scripts/dev_inject.sh"):
        "the operator-only dev rail naming the script it did not find",
}


def _shipped_py():
    for top in SHIPPED:
        base = ROOT / top
        if base.is_dir():
            for f in sorted(base.rglob("*.py")):
                rel = f.relative_to(ROOT)
                if "tests" not in rel.parts and "__pycache__" not in rel.parts:
                    yield rel


def _instructions(tree):
    docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                  and n.body and isinstance(n.body[0], ast.Expr)
                  and isinstance(n.body[0].value, ast.Constant)}
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings:
            for m in _PRIVATE_PATH.finditer(n.value):
                yield n.lineno, m.group(0)


def test_shipped_strings_name_no_private_file():
    bad = []
    for rel in _shipped_py():
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        for line, path in _instructions(tree):
            if (rel.as_posix(), path) not in ALLOWED:
                bad.append(f"{rel}:{line}: {path}")
    assert not bad, (
        "shipped runtime text names a private scripts/ or deployment/ file a public "
        f"user cannot run — name a CLI verb or a docs/guide page instead: {bad}")


def test_every_allowed_row_still_matches():
    seen = set()
    for rel in _shipped_py():
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        seen |= {(rel.as_posix(), p) for _, p in _instructions(tree)}
    stale = sorted(k for k in ALLOWED if k not in seen)
    assert not stale, f"drop these from ALLOWED: {stale}"
