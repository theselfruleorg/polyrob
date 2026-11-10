"""112 W1: a host job reaches the loops section (health) and <operations> (the agent)."""
from datetime import datetime, timezone

from core import host_jobs as hj
from core.status_loops import _host_job_lines
from core.status_render import render_operations_block
from core.status_types import Section

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def _loops(tmp_path, now=NOW):
    sec = Section(name="loops")
    _host_job_lines(sec, str(tmp_path), now.timestamp())
    return sec


def test_no_host_jobs_adds_no_line_and_no_health(tmp_path):
    sec = _loops(tmp_path)
    assert sec.data["host_jobs"] == [] and not sec.health
    work = Section(name="work")
    work.data.update(open_goals=[], stops=[])
    block = render_operations_block({"loops": sec, "work": work}, now=NOW.timestamp())
    assert "Host jobs" not in block


def test_failed_job_is_a_health_item_and_an_operations_line(tmp_path):
    hj.write_status(str(tmp_path), "collect", purpose="reads a feed, writes out/",
                    ok=False, max_age_h=3, errors=["boom"], outputs=["out/entries.jsonl"],
                    now=NOW)
    sec = _loops(tmp_path)
    assert [h.key for h in sec.health] == ["host_job:collect"]
    work = Section(name="work")
    work.data.update(open_goals=[], stops=[])
    block = render_operations_block({"loops": sec, "work": work}, now=NOW.timestamp())
    assert "- collect: reads a feed, writes out/ · failed (boom) · outputs: out/entries.jsonl" in block
    assert len(block.splitlines()) <= 25


def test_unreadable_dir_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(hj, "read_statuses",
                        lambda home, now=None: {"available": False, "error": "denied", "jobs": []})
    sec = _loops(tmp_path)
    assert sec.data["host_jobs"] is None and sec.health[0].key == "host_jobs_unreadable"
    work = Section(name="work")
    work.data.update(open_goals=[], stops=[])
    block = render_operations_block({"loops": sec, "work": work}, now=NOW.timestamp())
    assert "could not read (denied)" in block
