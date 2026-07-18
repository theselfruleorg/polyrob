"""P5b (proposal 018): raw numeric env parses may only SHRINK.

``int(os.getenv(...))`` / ``float(os.getenv(...))`` with no guard raises
ValueError on a stray ``"none"``/``"off"`` value — at an import-frozen site
that kills the whole process at startup (the class the 018 P5a migration
fixed in agents/task/constants.py + core/tickers.py). The canonical parsers
are ``core.env.int_env`` / ``core.env.float_env``. This ratchet freezes the
remaining raw-site count (65 at 2026-07-18) and forbids growth; lower it as
sites migrate, never raise it.
"""
import pathlib
import re

_BASELINE = 65
_SKIP = {"tests", ".git", "node_modules", ".venv", "venv", "__pycache__",
         "deployment", "docs", "scripts"}
_PAT = re.compile(r"(?:int|float)\(os\.getenv\(")


def test_raw_numeric_env_parse_count_never_grows():
    repo = pathlib.Path(__file__).resolve().parents[1]
    count = 0
    offenders = {}
    for p in repo.rglob("*.py"):
        rel = p.relative_to(repo)
        if _SKIP.intersection(rel.parts):
            continue
        try:
            hits = len(_PAT.findall(p.read_text(errors="ignore")))
        except OSError:
            continue
        if hits:
            count += hits
            offenders[str(rel)] = hits
    assert count <= _BASELINE, (
        f"raw int()/float()(os.getenv(...)) sites grew to {count} "
        f"(baseline {_BASELINE}). Use core.env.int_env/float_env instead. "
        f"Offenders: {offenders}")
