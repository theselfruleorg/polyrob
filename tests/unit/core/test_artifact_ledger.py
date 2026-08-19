"""The artifact ledger: a produced file gets a stable id, a hash, and a verdict.

Last week's goal runs failed three ways that all reduce to "nobody owns the
file": acceptance checks did `file_contains` on a relative path that a wipe had
invalidated, the completion judge compared a byte count the agent itself
claimed, and `deliverables.py` guessed attribution from a time window on a
shared workspace. A record written at write time answers all three.
"""
import os

import pytest

from core.artifacts import ArtifactLedger


@pytest.fixture
def ledger(tmp_path):
    return ArtifactLedger(db_path=str(tmp_path / "artifacts.db"))


def _write(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body)
    return str(p)


def test_record_captures_hash_and_size(ledger, tmp_path):
    path = _write(tmp_path, "report.md", "hello world")

    row = ledger.record("rob", path, session_id="s1", goal_id="g1", kind="report")

    assert row.id
    assert row.bytes == 11
    assert len(row.sha256) == 64
    assert row.url is None


def test_verify_reports_ok_missing_and_changed(ledger, tmp_path):
    path = _write(tmp_path, "a.md", "original")
    row = ledger.record("rob", path, session_id="s1", goal_id="g1")

    assert ledger.verify(row.id, "rob") == "ok"

    with open(path, "w") as f:
        f.write("tampered")
    assert ledger.verify(row.id, "rob") == "changed"

    os.remove(path)
    assert ledger.verify(row.id, "rob") == "missing"


def test_verify_is_unknown_for_another_tenant(ledger, tmp_path):
    path = _write(tmp_path, "a.md", "x")
    row = ledger.record("rob", path, session_id="s1", goal_id="g1")

    assert ledger.get(row.id, "someone_else") is None
    assert ledger.verify(row.id, "someone_else") == "unknown"


def test_list_for_goal_returns_only_that_goals_artifacts(ledger, tmp_path):
    a = ledger.record("rob", _write(tmp_path, "a.md", "a"), session_id="s1", goal_id="g1")
    ledger.record("rob", _write(tmp_path, "b.md", "b"), session_id="s1", goal_id="g2")

    rows = ledger.list_for_goal("rob", "g1")

    assert [r.id for r in rows] == [a.id]


def test_rewriting_a_path_updates_the_row_instead_of_duplicating(ledger, tmp_path):
    path = _write(tmp_path, "a.md", "v1")
    first = ledger.record("rob", path, session_id="s1", goal_id="g1")

    with open(path, "w") as f:
        f.write("v2 is longer")
    second = ledger.record("rob", path, session_id="s1", goal_id="g1")

    assert second.id == first.id
    assert second.bytes == 12
    assert len(ledger.list_for_goal("rob", "g1")) == 1
    assert ledger.verify(first.id, "rob") == "ok"


def test_set_url_publishes_the_artifact(ledger, tmp_path):
    row = ledger.record("rob", _write(tmp_path, "a.html", "<html>"), session_id="s1", goal_id="g1")

    ledger.set_url(row.id, "rob", "https://pub.example.com/a/")

    assert ledger.get(row.id, "rob").url == "https://pub.example.com/a/"


def test_set_url_refuses_another_tenant(ledger, tmp_path):
    row = ledger.record("rob", _write(tmp_path, "a.html", "<html>"), session_id="s1", goal_id="g1")

    assert ledger.set_url(row.id, "someone_else", "https://evil.example.com/") is False
    assert ledger.get(row.id, "rob").url is None


def test_record_of_a_missing_file_is_refused(ledger, tmp_path):
    assert ledger.record("rob", str(tmp_path / "nope.md"), session_id="s1", goal_id="g1") is None
