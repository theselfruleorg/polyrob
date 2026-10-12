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

from tests.support.repo_index import repo_index

# 65 (2026-07-18, narrow pattern) → 93 (2026-08-09): the widened pattern newly
# counts the `environ.get`/`env.get` shapes it used to miss. Shrink-only, as ever.
# 93 → 78 (2026-10-06): x402 invoice caps + code-exec limits moved to resolvers.
_BASELINE = 51  # 2026-10-10: 78 -> 76 -> 51 (61 tuning knobs became module constants); tightness test below.
_SKIP = {"tests", ".git", "node_modules", ".venv", "venv", "__pycache__",
         "deployment", "docs", "scripts",
         # generated build artifacts (a stale wheel tree double-counts every
         # site and false-fails the ratchet after a release build)
         "build", "dist"}
# Widened 2026-08-09: the original shape missed `float(os.environ.get(...))`
# and `float(env.get(...))` (injected-Mapping style) — core/wallet/config.py's
# AGENT_WALLET_MAX_PER_TX_USD parse was invisible to the ratchet.
_PAT = re.compile(r"(?:int|float)\(\s*(?:os\.)?(?:getenv|environ\.get|env\.get)\(")


def _count():
    repo = pathlib.Path(__file__).resolve().parents[1]
    count = 0
    offenders = {}
    for p in repo.rglob("*.py"):
        rel = p.relative_to(repo)
        if _SKIP.intersection(rel.parts):
            continue
        try:
            hits = len(_PAT.findall(repo_index().text(p, errors="ignore")))
        except OSError:
            continue
        if hits:
            count += hits
            offenders[str(rel)] = hits
    return count, offenders


def test_raw_numeric_env_parse_count_never_grows():
    count, offenders = _count()
    assert count <= _BASELINE, (
        f"raw int()/float()(os.getenv(...)) sites grew to {count} "
        f"(baseline {_BASELINE}). Use core.env.int_env/float_env instead. "
        f"Offenders: {offenders}")


def test_baseline_tracks_actual():
    """Tightness: a migration that forgets to lower _BASELINE stops the ratchet."""
    count, offenders = _count()
    assert count >= _BASELINE, (
        f"raw numeric env parses fell to {count}; lower _BASELINE ({_BASELINE}) "
        f"to {count}.")
