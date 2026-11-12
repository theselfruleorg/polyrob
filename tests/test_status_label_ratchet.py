"""Label and seat-parity ratchet for the status snapshot (proposal 116 W10.2, W10.3).

The 2026-11-11 status review found one word printed for two numbers, "today"
on a rolling 24 h count, and counts with no window at all. This test renders
ONE seeded home and holds three rules over the text every seat prints:

1. No "today": every window in the snapshot is a rolling one and is called
   "24h" (or names its own window). A calendar day is the owner's day and only
   the digest header computes it.
2. A count line names its window and, where the reader could confuse two
   stores, its store.
3. No label is printed twice in one render (one word, one number).

And the parity rule: the seats render the SAME lines from the same snapshot,
and a seat that cannot read a fact says so instead of printing "none".
"""
import re

import pytest

from tests.unit.core import test_status_snapshot as seeded

degraded = seeded.degraded          # the seeded home (a pytest fixture)

#: Lines that carry a count over a window: the prefix, then a token that names it.
_WINDOWED = (
    ("owner messages", "24h"),
    ("missed owner messages", "in 24h"),
    ("24h goal runs ended", "24h"),
    ("24h job runs ended", "24h"),
    ("24h governance", "24h"),
    ("24h:", "24h"),
)


@pytest.fixture()
def rendered(degraded):
    from core.status_render import render_status_text
    snap = seeded.build_status_snapshot(seeded.OWNER, data_dir=degraded)
    return snap, render_status_text(snap, title="Status:", prefix="• ")


def _body(line: str) -> str:
    return line.lstrip("• ").strip()


def test_no_today_on_a_rolling_window(rendered):
    _snap, text = rendered
    bad = [l for l in text.splitlines() if re.search(r"\btoday\b", l, re.I)]
    assert not bad, "a rolling 24 h count is called '24h', never 'today':\n" + "\n".join(bad)


def test_a_count_line_names_its_window(rendered):
    _snap, text = rendered
    lines = [_body(l) for l in text.splitlines()]
    for prefix, token in _WINDOWED:
        hits = [l for l in lines if l.split(": ", 1)[-1].startswith(prefix) or l.startswith(prefix)]
        for l in hits:
            assert token in l, f"{prefix!r} line names no window: {l}"
    # The two delivery counts name their store (the event log vs the thread).
    owner = next(l for l in lines if "owner messages 24h" in l)
    assert "[telemetry_events.db]" in owner


def test_one_label_is_printed_once(rendered):
    """Two lines with one label and two numbers is the P1 defect (`done=68`
    beside `done=22`). A label is the text before the first colon."""
    _snap, text = rendered
    seen = {}
    for raw in text.splitlines()[1:]:
        body = _body(raw)
        if ":" not in body or body.startswith(("⚠", "⛔", "job ", "Health", "▶", "⏸", "■")):
            continue
        if raw.startswith("•     "):
            continue                          # a list row under a header line
        label = body.split(":", 1)[0]
        if not re.search(r"\d", body.split(":", 1)[1]):
            continue                          # no number: nothing to confuse
        seen.setdefault(label, []).append(body)
    twice = {k: v for k, v in seen.items() if len(v) > 1}
    assert not twice, f"one label, two numbers: {twice}"


def test_done_says_what_it_did_on_both_run_lines(rendered):
    _snap, text = rendered
    for head in ("24h goal runs ended", "24h job runs ended"):
        line = next(l for l in text.splitlines() if head in l)
        if "done=" in line:
            assert re.search(r"done=\d+ \(", line), line


# --- W10.3: seat parity --------------------------------------------------------

def test_the_seats_print_the_same_lines(rendered):
    from core.status_render import (health_headline, render_agent_health_note,
                                    render_health_lines, render_section_lines)
    snap, chat = rendered
    chat_lines = chat.splitlines()
    doctor = render_health_lines(snap, prefix="  ")
    console = render_health_lines(snap, prefix="")
    # The headline is byte-equal on every seat, and it is the first health line.
    head = health_headline(snap)
    assert doctor[0] == console[0] == head and head in chat_lines
    # Every health item reaches every seat with the same words.
    note = render_agent_health_note(snap, max_items=50)
    for h in snap.health:
        for seat, body in (("chat", chat), ("doctor", "\n".join(doctor)),
                           ("console", "\n".join(console))):
            assert h.text in body, f"{seat} lost: {h.text}"
        assert h.text[:60] in note, f"agent_status lost: {h.text}"
    # A section renders the same lines under one prefix, whoever asks.
    for name in ("work", "loops", "delivery"):
        for l in render_section_lines(snap, name, prefix="• "):
            assert l in chat_lines, f"{name}: {l}"


def test_a_seat_that_cannot_read_a_fact_says_so(rendered):
    snap, text = rendered                      # built with no agent and no container
    assert "no session on this seat (not checked)" in text
    assert "Tool gates: not read here" in text
    assert "no active session" not in text
    from core.status_render import checked_list
    assert "surface circuits" not in checked_list(snap)
