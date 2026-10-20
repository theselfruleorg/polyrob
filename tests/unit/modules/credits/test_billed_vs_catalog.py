"""A-1 / B-9 (review 2026-10-14): `polyrob doctor` shows billed vs catalog cost.

For 12 days prod paid exactly 2.00x the catalog price and nothing showed it.
"""
import asyncio
import json
import sqlite3

import pytest

from modules.credits import usage_tracker as ut


def _db(tmp_path, rows):
    path = tmp_path / "bot.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE usage_records (resource_type TEXT, metadata TEXT, timestamp TEXT)")
    for meta, ts in rows:
        con.execute("INSERT INTO usage_records VALUES ('llm_call', ?, COALESCE(?, CURRENT_TIMESTAMP))",
                    (json.dumps(meta), ts))
    con.commit()
    con.close()
    return str(path)


def test_ratio_over_the_last_day_only(tmp_path):
    path = _db(tmp_path, [
        ({"billed_cost_usd": 0.02, "estimated_cost_usd": 0.01}, None),
        ({"billed_cost_usd": 0.02, "estimated_cost_usd": 0.01}, None),
        ({"estimated_cost_usd": 0.5}, None),                                  # no billed figure
        ({"billed_cost_usd": 9.0, "estimated_cost_usd": 1.0}, "2020-01-01 00:00:00"),  # old
    ])
    data = ut.billed_vs_catalog(path)
    assert data["calls"] == 2 and data["ratio"] == pytest.approx(2.0)
    line = ut.billed_vs_catalog_line(path)
    assert line.startswith("! billed vs catalog cost, last 24 h: x2.00")


def test_in_band_ratio_is_not_a_warning(tmp_path):
    path = _db(tmp_path, [({"billed_cost_usd": 0.0104, "estimated_cost_usd": 0.01}, None)])
    line = ut.billed_vs_catalog_line(path)
    assert line.startswith("billed vs catalog cost, last 24 h: x1.04")


def test_low_ratio_warns(tmp_path):
    path = _db(tmp_path, [({"billed_cost_usd": 0.005, "estimated_cost_usd": 0.01}, None)])
    assert ut.billed_vs_catalog_line(path).startswith("! ")


def test_empty_missing_and_unreadable_are_told_apart(tmp_path):
    assert "no calls with a billed cost" in ut.billed_vs_catalog_line(_db(tmp_path, []))
    assert "no bot.db yet" in ut.billed_vs_catalog_line(str(tmp_path / "nope.db"))
    bad = tmp_path / "bad.db"
    bad.write_text("not a database")
    assert "unreadable" in ut.billed_vs_catalog_line(str(bad))


def test_record_keeps_both_figures_even_when_billed_equals_the_estimate():
    """The ratio must not be biased toward the rows that disagree."""
    tracker = ut.LLMUsageTracker.__new__(ut.LLMUsageTracker)
    costs = ut.CostBreakdown(api_cost_usd=0.01, markup_multiplier=1.0, credits_raw=1.0,
                             credits_charged=1, user_cost_usd=0.01)
    assert tracker._apply_billed_cost(costs, 0.01) is not costs
    assert tracker._apply_billed_cost(costs, None) is costs
