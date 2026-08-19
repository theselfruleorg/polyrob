"""A published deliverable is reported by its URL, not by a server path.

The 2026-07-19 usability assessment's core failure mode was a bare filename the
owner could not reach; the answer then was "attached" or "server-only: <path>".
Once a file has actually been PUBLISHED, neither is the useful thing to say — a
URL is. This closes the ship rail end to end: build -> publish -> the owner gets
a link.
"""
from agents.task.goals.deliverables import deliverable_line_for


def test_a_published_file_is_reported_by_its_url():
    line = deliverable_line_for("index.html", "1.2 KB",
                                url="https://pub.example.com/rob-status/")

    assert "https://pub.example.com/rob-status/" in line
    assert "server-only" not in line


def test_an_unpublished_file_keeps_the_existing_wording():
    line = deliverable_line_for("notes.md", "800 B", url=None,
                                fallback="- notes.md (800 B) — server-only: /ws/notes.md (x)")

    assert line == "- notes.md (800 B) — server-only: /ws/notes.md (x)"
