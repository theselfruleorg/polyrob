"""112 W1: host-job status files — written atomically, read honestly."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

from core import host_jobs as hj

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def _write(home, name="collect", **kw):
    args = dict(purpose="reads a feed", ok=True, max_age_h=3, errors=[], outputs=["out/a.jsonl"],
                now=NOW)
    args.update(kw)
    return hj.write_status(str(home), name, **args)


def test_missing_dir_is_no_host_jobs(tmp_path):
    assert hj.read_statuses(str(tmp_path), NOW) == {"available": True, "error": "", "jobs": []}


def test_valid_file_is_ok_and_mode_is_group_readable(tmp_path):
    path = _write(tmp_path)
    assert oct(os.stat(path).st_mode & 0o777) == oct(hj.FILE_MODE)
    (row,) = hj.read_statuses(str(tmp_path), NOW + timedelta(hours=1))["jobs"]
    assert row["state"] == hj.OK and row["outputs"] == ["out/a.jsonl"] and row["why"] == ""


def test_stale_failed_and_skewed(tmp_path):
    _write(tmp_path, "old")
    _write(tmp_path, "bad", ok=False, errors=["read error: 429"])
    _write(tmp_path, "future", now=NOW + timedelta(hours=1))
    rows = {r["name"]: r for r in hj.read_statuses(str(tmp_path), NOW + timedelta(hours=4))["jobs"]}
    assert rows["old"]["state"] == hj.STALE and "no run since 2026-11-09T12:00:00Z" in rows["old"]["why"]
    assert rows["bad"]["state"] == hj.FAILED and "429" in rows["bad"]["why"]
    assert rows["future"]["state"] == hj.STALE or rows["future"]["state"] == hj.OK
    rows = {r["name"]: r for r in hj.read_statuses(str(tmp_path), NOW)["jobs"]}
    assert rows["future"]["state"] == hj.SKEWED


@pytest.mark.parametrize("mutate,why", [
    (lambda r: r.pop("ok"), "missing ok"),
    (lambda r: r.update(name="other"), "name does not match"),
    (lambda r: r.update(outputs=["../etc/passwd"]), "not relative"),
    (lambda r: r.update(outputs=["/abs"]), "not relative"),
    (lambda r: r.update(max_age_h=0), "max_age_h"),
    (lambda r: r.update(ok="yes"), "true/false"),
])
def test_invalid_record_is_an_unreadable_row(tmp_path, mutate, why):
    path = _write(tmp_path)
    rec = json.load(open(path))
    mutate(rec)
    json.dump(rec, open(path, "w"))
    (row,) = hj.read_statuses(str(tmp_path), NOW)["jobs"]
    assert row["state"] == hj.UNREADABLE and why in row["why"]


def test_torn_file_is_unreadable_not_skipped(tmp_path):
    _write(tmp_path)
    open(os.path.join(hj.status_dir(str(tmp_path)), "collect.json"), "w").write("{")
    (row,) = hj.read_statuses(str(tmp_path), NOW)["jobs"]
    assert row["state"] == hj.UNREADABLE and "cannot read" in row["why"]


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX non-root")
def test_unlistable_dir_is_unavailable(tmp_path):
    _write(tmp_path)
    folder = hj.status_dir(str(tmp_path))
    os.chmod(folder, 0)
    try:
        read = hj.read_statuses(str(tmp_path), NOW)
    finally:
        os.chmod(folder, 0o755)
    assert read["available"] is False and "cannot list" in read["error"]


def test_bad_name_is_refused_and_text_is_clipped(tmp_path):
    with pytest.raises(ValueError):
        _write(tmp_path, "Bad Name")
    _write(tmp_path, purpose="x\n" * 500, errors=["e"] * 9)
    (row,) = hj.read_statuses(str(tmp_path), NOW)["jobs"]
    assert len(row["purpose"]) <= hj.TEXT_MAX and "\n" not in row["purpose"]


def test_core_names_no_job():
    src = open(hj.__file__).read()
    for word in ("contest", "bounty", "x_contest", "twitter"):
        assert word not in src.lower()
