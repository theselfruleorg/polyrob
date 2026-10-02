"""Flag retirement (067 F3): "one release later" is enforced, not promised.

A revert flag or a legacy alias carries ``Retire after X.Y`` in its
``docs/CONFIGURATION.md`` row; ``scripts/gen_flags_catalog.py`` lifts the marker
into ``core.flags_catalog.RETIRE_AFTER``. Once ``core/version.py`` reaches X.Y
this test fails until the flag is gone: delete the row and its reader (keep the
new behaviour), or — with a reason in the commit — move the marker to a later
release. Reads the shipped catalog, never the doc (the doc is sdist-only).
"""
import re

from core.flags_catalog import CATALOG, RETIRE_AFTER
from core.version import __version__

_MARK_RE = re.compile(r"^\d+\.\d+$")


def _major_minor(version: str) -> tuple:
    parts = re.findall(r"\d+", version)
    return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0


def test_no_flag_outlives_its_retirement():
    current = _major_minor(__version__)
    overdue = {name: mark for name, mark in RETIRE_AFTER.items()
               if current >= _major_minor(mark)}
    assert not overdue, (
        f"version {__version__} reached the retirement marker of {len(overdue)} "
        f"flag(s): {overdue}. Delete each row (and its reader, keeping the new "
        "behaviour), or move its 'Retire after X.Y' marker in "
        "docs/CONFIGURATION.md with the reason in the commit; then run BOTH "
        "generators and lower CEILING in tests/test_flag_count_ratchet.py."
    )


def test_markers_are_well_formed_and_name_real_rows():
    names = {row[0] for row in CATALOG}
    assert RETIRE_AFTER, "no retirement markers parsed — generator or doc broke"
    bad = {n: m for n, m in RETIRE_AFTER.items()
           if not _MARK_RE.match(m) or n not in names}
    assert not bad, f"malformed marker or unknown flag: {bad}"


def test_retiring_flags_are_internal_tier():
    tier = {row[0]: row[4] for row in CATALOG}
    wrong = sorted(n for n in RETIRE_AFTER if tier.get(n) != "internal")
    assert not wrong, f"a flag scheduled for removal must be tier internal: {wrong}"
