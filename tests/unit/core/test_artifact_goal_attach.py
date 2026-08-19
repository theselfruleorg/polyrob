"""The goal dispatcher stamps a finished run's artifacts with the goal id.

The tools tier must not know what a goal is (layering ratchet), so a write
records (user_id, session_id) only. The dispatcher owns the goal<->session
mapping and stamps attribution at run end.
"""
import pytest

from core.artifacts import ArtifactLedger


@pytest.fixture
def ledger(tmp_path):
    return ArtifactLedger(db_path=str(tmp_path / "artifacts.db"))


def _write(tmp_path, name, body="x"):
    p = tmp_path / name
    p.write_text(body)
    return str(p)


def test_attach_goal_stamps_that_sessions_artifacts(ledger, tmp_path):
    ledger.record("rob", _write(tmp_path, "a.md"), session_id="s1")
    ledger.record("rob", _write(tmp_path, "b.md"), session_id="s1")
    ledger.record("rob", _write(tmp_path, "c.md"), session_id="s2")

    stamped = ledger.attach_goal("rob", "s1", "g1")

    assert stamped == 2
    assert {r.path.split("/")[-1] for r in ledger.list_for_goal("rob", "g1")} == {"a.md", "b.md"}


def test_attach_goal_never_crosses_tenants(ledger, tmp_path):
    ledger.record("rob", _write(tmp_path, "a.md"), session_id="s1")

    assert ledger.attach_goal("someone_else", "s1", "g1") == 0
    assert ledger.list_for_goal("rob", "g1") == []


def test_attach_goal_does_not_resteal_an_already_attributed_artifact(ledger, tmp_path):
    ledger.record("rob", _write(tmp_path, "a.md"), session_id="s1", goal_id="g1")

    assert ledger.attach_goal("rob", "s1", "g2") == 0
    assert len(ledger.list_for_goal("rob", "g1")) == 1
