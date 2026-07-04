"""D1: the turn-summary segment logic is extracted to ONE pure function.

Previously 3 copies: blocks.turn_summary_line, plain_renderer._print_turn_summary,
and rich_renderer (via blocks). dialog.summary_segments is now the SSOT; the
zero-omission rules, cost threshold ($0.00005), pluralization, and 1s elapsed
floor live in one place.
"""

from __future__ import annotations

from cli.ui import dialog


def test_all_segments_present():
    segs = dialog.summary_segments(
        steps=3, tools=2, tokens=14200, cost=0.0040, elapsed_seconds=28.0, failed=False
    )
    assert segs == ["3 steps", "2 tools", "14.2k tok", "$0.0040", "28s"]


def test_singular_pluralization():
    segs = dialog.summary_segments(
        steps=1, tools=1, tokens=0, cost=0.0, elapsed_seconds=0.0, failed=False
    )
    assert "1 step" in segs and "1 steps" not in segs
    assert "1 tool" in segs and "1 tools" not in segs


def test_zero_segments_omitted():
    segs = dialog.summary_segments(
        steps=0, tools=0, tokens=0, cost=0.0, elapsed_seconds=0.0, failed=False
    )
    assert segs == []


def test_cost_below_threshold_omitted():
    # $0.00004 rounds to $0.0000 → omitted; $0.00005 kept.
    assert not any("$" in s for s in dialog.summary_segments(
        steps=1, tools=0, tokens=0, cost=0.00004, elapsed_seconds=0.0, failed=False))
    assert any("$" in s for s in dialog.summary_segments(
        steps=1, tools=0, tokens=0, cost=0.00005, elapsed_seconds=0.0, failed=False))


def test_elapsed_below_one_second_omitted():
    assert not any("s" in s and s.endswith("s") and s[0].isdigit()
                   for s in dialog.summary_segments(
        steps=1, tools=0, tokens=0, cost=0.0, elapsed_seconds=0.4, failed=False))


def test_failed_segment():
    segs = dialog.summary_segments(
        steps=2, tools=0, tokens=0, cost=0.0, elapsed_seconds=0.0, failed=True
    )
    assert "failed" in segs
