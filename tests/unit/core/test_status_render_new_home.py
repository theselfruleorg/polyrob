"""A new install is not a broken one, and a broken one must not read as new."""
from core.status_render import health_headline, render_section_lines
from core.status_snapshot import OVERALL_PARTIAL, STATE_UNAVAILABLE, Section, StatusSnapshot
from core.status_types import SECTION_ORDER


def _snap(reasons):
    sections = {n: Section(name=n) for n in SECTION_ORDER}
    for name, reason in reasons.items():
        sections[name] = Section(name=name, state=STATE_UNAVAILABLE, reason=reason)
    snap = StatusSnapshot(user_id="u", generated_at=0.0, window_sec=60, sections=sections, health=[],
                          unavailable_sources=[f"{n} ({r})" for n, r in reasons.items()])
    assert snap.overall == OVERALL_PARTIAL
    return snap


ABSENT = "FileNotFoundError: goals.db not found at /h/data/goals.db"
ALL_ABSENT = {"work": ABSENT, "delivery": "RuntimeError: x (FileNotFoundError: telemetry_events.db not found at /h/data/t.db)",
              "security": "FileNotFoundError: telemetry_events.db not found at /h/data/t.db"}


def test_every_store_absent_reads_as_new_and_names_the_other_reading():
    head = health_headline(_snap(ALL_ABSENT))
    assert head.startswith("Health: NEW")
    assert "/h/data" in head and "data dir" in head


def test_a_new_home_section_says_nothing_yet():
    line = render_section_lines(_snap(ALL_ABSENT), "work")[0]
    assert "nothing yet (goals.db not created)" in line
    assert "FileNotFoundError" not in line


def test_an_unreadable_store_stays_partial():
    snap = _snap({**ALL_ABSENT, "security": "OperationalError: database disk image is malformed"})
    assert health_headline(snap).startswith("Health: PARTIAL")
    assert "unavailable (FileNotFoundError" in render_section_lines(snap, "work")[0]


def test_one_absent_store_beside_readable_ones_stays_partial(monkeypatch):
    # With autonomy on, something should have made the store.
    monkeypatch.setattr("core.status_loops.autonomy_idle_reason", lambda: "")
    assert health_headline(_snap({"work": ABSENT})).startswith("Health: PARTIAL")
