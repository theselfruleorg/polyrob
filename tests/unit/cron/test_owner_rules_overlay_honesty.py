"""B12 (2026-10-13 memory/coherency review): the newer-owner-rules overlay
names what it cannot show — undated lines, a cut list, and an unreadable doc.
"""
from datetime import datetime
from types import SimpleNamespace

from cron import owner_rules_overlay as ov

DOC = """NEW RULE (2026-10-05): stop posting rebuys. [from: owner said 2026-10-05]

UNDATED RULE with no stamp at all.
ANOTHER undated line.
"""


def test_undated_lines_are_counted_not_dropped_silently():
    block = ov.owner_rules_overlay(DOC, datetime(2026, 10, 1))
    assert "stop posting rebuys" in block
    assert "2 owner-doc line(s) carry no date" in block


def test_cut_list_says_how_many_were_omitted():
    many = "\n".join(f"R{i} (2026-10-05): x [from: owner said 2026-10-05]" for i in range(11))
    block = ov.owner_rules_overlay(many, datetime(2026, 10, 1))
    assert block.count("\n- ") == ov.MAX_RULES
    assert "3 more newer owner rule(s) omitted" in block


def test_unreadable_doc_is_not_rendered_as_nothing(monkeypatch):
    import core.instance as inst
    monkeypatch.setattr(inst, "load_owner_doc",
                        lambda *a, **k: inst.UNREADABLE_PREFIX + "owner.md: EACCES]")
    job = SimpleNamespace(user_id="u1", created_at=datetime(2026, 10, 1))
    out = ov.overlay_for_job(job, "/nonexistent")
    assert out.startswith("OWNER RULES: could not read")


def test_read_error_is_not_rendered_as_nothing(monkeypatch):
    import core.instance as inst

    def boom(*a, **k):
        raise OSError("disk")
    monkeypatch.setattr(inst, "load_owner_doc", boom)
    job = SimpleNamespace(user_id="u1", created_at=datetime(2026, 10, 1))
    assert "could not read" in ov.overlay_for_job(job, "/nonexistent")


def test_absent_doc_stays_empty(monkeypatch):
    import core.instance as inst
    monkeypatch.setattr(inst, "load_owner_doc", lambda *a, **k: "")
    job = SimpleNamespace(user_id="u1", created_at=datetime(2026, 10, 1))
    assert ov.overlay_for_job(job, "/nonexistent") == ""
