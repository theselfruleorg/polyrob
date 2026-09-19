"""Migrations run in NUMERIC version order, never filename order.

``sorted(glob("v*.py"))`` puts ``v1_10_0_*`` BEFORE ``v1_2_0_*``. The tree sat at
``v1_9_0`` for months, so the first two-digit minor would have run out of order on
every fresh install and every behind-by-two upgrade — green in CI, because no test
ever asserted the order with a two-digit component present.
"""
from pathlib import Path

from migrations.version_manager import latest_migration_version, shipped_migrations


def _touch(d: Path, *names: str) -> None:
    for n in names:
        (d / n).write_text("async def upgrade(db):\n    pass\n")


def test_two_digit_minor_sorts_after_single_digit(tmp_path):
    _touch(tmp_path, "v1_10_0_c.py", "v1_2_0_a.py", "v1_9_0_b.py", "v1_9_1_bb.py")
    assert [v for v, _ in shipped_migrations(tmp_path)] == ["1.2.0", "1.9.0", "1.9.1", "1.10.0"]


def test_order_agrees_with_latest_migration_version(tmp_path):
    _touch(tmp_path, "v1_10_0_c.py", "v1_2_0_a.py", "v2_0_0_z.py", "v1_9_0_b.py")
    assert shipped_migrations(tmp_path)[-1][0] == latest_migration_version(tmp_path) == "2.0.0"


def test_non_migration_files_are_ignored(tmp_path):
    _touch(tmp_path, "v1_1_0_x.py", "vhelpers.py", "__init__.py")
    assert [v for v, _ in shipped_migrations(tmp_path)] == ["1.1.0"]


def test_the_real_tree_is_already_in_numeric_order():
    shipped = shipped_migrations()
    keys = [tuple(int(x) for x in v.split(".")) for v, _ in shipped]
    assert keys == sorted(keys)
