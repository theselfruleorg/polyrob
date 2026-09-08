"""Every declared extra must be RESOLVABLE, not merely well-intentioned.

0.13.0, public CI: `pip install -e ".[dev,all]"` failed with a Cython internal
error building a `cytoolz` sdist. cytoolz was innocent — the `solana` extra
declared `x402[svm]>=2.16.0` together with `solana>=0.34,<0.36`, and EVERY
published x402 requires `solana>=0.36.0` under its `svm` extra. The pair had no
solution at all, so pip backtracked through the whole x402 range and eventually
reached a version old enough to be sdist-only, whose build then failed. The
install error named the wrong package entirely.

These tests read the declared pins as data. They are offline and cheap; a real
resolution is what CI does, and this is the guard that keeps CI's failure legible.
"""

import re
import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]


def _extras() -> dict[str, list[str]]:
    with (_ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["optional-dependencies"]


def _bound(specs: list[str], name: str, op: str) -> str | None:
    """Return the version in the first `<name><op><version>` clause, or None."""
    for spec in specs:
        base = spec.split(";")[0]
        pkg = re.split(r"[<>=!\[]", base, 1)[0].strip()
        if pkg != name:
            continue
        m = re.search(rf"{re.escape(op)}\s*([0-9][0-9.]*)", base)
        if m:
            return m.group(1)
    return None


def _tuple(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split(".") if p.isdigit())


class TestSolanaExtra:
    def test_solana_floor_admits_the_x402_svm_requirement(self):
        """x402's `svm` extra requires solana>=0.36.0 in every published release.

        A floor below that is not merely loose — combined with our own upper
        bound it makes the extra unsatisfiable, and pip reports the failure
        against whatever transitive sdist it stumbles into while backtracking.
        """
        specs = _extras()["solana"]
        floor = _bound(specs, "solana", ">=")
        assert floor is not None, "the solana pin must declare a floor"
        assert _tuple(floor) >= (0, 36), (
            f"solana floor {floor} is below the 0.36.0 that x402[svm] requires; "
            "with the <0.40 ceiling this extra has NO solution"
        )

    def test_solana_ceiling_still_excludes_the_sync_client_removal(self):
        """The ceiling is load-bearing, not tidiness.

        x402's SVM mechanism imports `from solana.rpc.api import Client` — the
        SYNCHRONOUS client. solana-py ships `rpc/api.py` through 0.39.0 and
        DELETED it in 0.40.0, so an install above the ceiling resolves to a tree
        where the SDK's own module cannot import.
        """
        ceiling = _bound(_extras()["solana"], "solana", "<")
        assert ceiling is not None, (
            "the solana pin must keep an upper bound: 0.40.0 removed the "
            "synchronous rpc.api.Client that x402[svm] imports"
        )
        assert _tuple(ceiling) <= (0, 40), (
            f"solana ceiling {ceiling} admits 0.40+, where rpc/api.py is gone"
        )

    def test_the_solana_window_is_non_empty(self):
        specs = _extras()["solana"]
        floor, ceiling = _bound(specs, "solana", ">="), _bound(specs, "solana", "<")
        assert _tuple(floor) < _tuple(ceiling), (
            f"solana window >={floor},<{ceiling} is empty — nothing can install"
        )


class TestExtrasWellFormed:
    def test_all_aggregates_every_shipping_extra(self):
        """`all` is what CI installs; an extra missing from it is untested."""
        extras = _extras()
        aggregated = set()
        for spec in extras["all"]:
            m = re.search(r"polyrob\[([^\]]+)\]", spec)
            if m:
                aggregated |= {p.strip() for p in m.group(1).split(",")}
        expected = set(extras) - {"all", "dev"}
        assert expected <= aggregated, (
            f"extras missing from `all` (so never resolved in CI): "
            f"{sorted(expected - aggregated)}"
        )

    @pytest.mark.parametrize("extra", sorted(set(_extras()) - {"all"}))
    def test_no_extra_declares_an_empty_version_window(self, extra):
        """A `>=X,<Y` with X >= Y can never resolve."""
        for spec in _extras()[extra]:
            base = spec.split(";")[0]
            lo = re.search(r">=\s*([0-9][0-9.]*)", base)
            hi = re.search(r"<\s*([0-9][0-9.]*)", base)
            if lo and hi:
                assert _tuple(lo.group(1)) < _tuple(hi.group(1)), (
                    f"{extra}: {base!r} declares an empty window"
                )
