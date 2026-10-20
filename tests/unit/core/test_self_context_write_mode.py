"""The writer's mkstemp temp file is 0600, and os.replace kept that mode, so
every promote left owner.md/self.md unreadable to the polyrob-data group
(prod 2026-10-15 04:47/05:06: both went 660 -> 600). An active doc is written
group-shared, 0660.
"""
import os
import stat

from core.self_context_writer import SelfContextWriter


def test_atomic_write_is_group_shared(tmp_path):
    w = SelfContextWriter(tmp_path, "rob")
    target = tmp_path / "doc.md"
    w._atomic_write(target, "hello\n")
    assert target.read_text() == "hello\n"
    assert stat.S_IMODE(os.stat(target).st_mode) & 0o060 == 0o060


def test_atomic_write_never_world_readable(tmp_path):
    w = SelfContextWriter(tmp_path, "rob")
    target = tmp_path / "doc.md"
    w._atomic_write(target, "x")
    assert stat.S_IMODE(os.stat(target).st_mode) & 0o007 == 0
