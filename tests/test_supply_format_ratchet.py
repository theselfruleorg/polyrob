"""No token supply is ever printed with ``{:g}`` (085 W7).

``{supply:g}`` rounds to six significant digits: 1234567 printed as
``1.23457e+06`` on the owner's approval, and the Telegram retry hint built from
it minted 1234570 whole units — a different token, with no mint function to
fix it. Print a supply with ``core.wallet.token_template.format_supply``.
"""
import pathlib
import re

from tests.support.repo_index import repo_index

_SKIP = {"tests", "ops", ".git", "node_modules", ".venv", "venv", "__pycache__",
         "build", "dist"}
#: ``{supply:g}``, ``{params.supply:.6g}``, ``{supply_whole:,g}`` …
_PAT = re.compile(r"supply[\w.\[\]'\"]*\s*:[^}\n]*g\}")


def _offenders():
    repo = pathlib.Path(__file__).resolve().parents[1]
    out = {}
    for p in repo.rglob("*.py"):
        rel = p.relative_to(repo)
        if _SKIP.intersection(rel.parts):
            continue
        try:
            text = repo_index().text(p, errors="ignore")
        except OSError:
            continue
        hits = _PAT.findall(text)
        if hits:
            out[str(rel)] = hits
    return out


def test_no_supply_is_formatted_with_g():
    offenders = _offenders()
    assert not offenders, (
        "a token supply is formatted with `:g`, which rounds it. Use "
        f"core.wallet.token_template.format_supply. Offenders: {offenders}")


def test_the_pattern_catches_the_shapes():
    for bad in ('f"{supply:g}"', 'f"{params.supply:g}"', 'f"{supply:.6g}"',
                'f"{supply_whole:,g}"'):
        assert _PAT.search(bad), bad
    for good in ('f"{format_supply(supply)}"', 'f"{supply_raw}"',
                 'f"{supply!r}"'):
        assert not _PAT.search(good), good
