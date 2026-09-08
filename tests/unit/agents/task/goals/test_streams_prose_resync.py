"""B26 (S9, 2026-08-29): an objective's title/body follow the stream manifest
UNLESS the owner edited the prose in-app — the owner's edit wins forever."""
import copy

import pytest

from agents.task.goals.board import GoalBoard
from agents.task.goals import streams as S
from tests.unit.agents.task.goals.test_streams import MANIFEST


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


def _stream(body, title="Demo objective"):
    m = copy.deepcopy(MANIFEST["streams"][0])
    m["objective"]["body"] = body
    m["objective"]["title"] = title
    return m


def test_manifest_prose_change_reaches_an_untouched_objective(board):
    oid, _ = S.ensure_objective(board, "u1", _stream("v1 body"))
    assert board.get(oid).body == "v1 body"
    oid2, note = S.ensure_objective(board, "u1", _stream("v2 body", title="Demo objective v2"))
    assert oid2 == oid
    g = board.get(oid)
    assert g.body == "v2 body" and g.title == "Demo objective v2"
    assert "prose re-synced" in note


def test_owner_edit_wins_over_later_manifest_changes(board):
    oid, _ = S.ensure_objective(board, "u1", _stream("v1 body"))
    assert board.update_fields(oid, user_id="u1", body="owner's own words")
    S.ensure_objective(board, "u1", _stream("v2 body"))
    assert board.get(oid).body == "owner's own words"
    S.ensure_objective(board, "u1", _stream("v3 body"))
    assert board.get(oid).body == "owner's own words"


def test_pre_feature_objective_is_stamped_but_not_clobbered(board):
    oid, _ = S.ensure_objective(board, "u1", _stream("v1 body"))
    # simulate a row seeded before the prose hash existed
    g = board.get(oid)
    payload = dict(g.payload); payload.pop("manifest_prose_hash", None)
    board.update_fields(oid, payload_patch={"manifest_prose_hash": None})
    S.ensure_objective(board, "u1", _stream("v2 body"))
    assert board.get(oid).body == "v1 body"           # unknown provenance: never overwrite
    assert board.get(oid).payload.get("manifest_prose_hash")  # ...but tracked from now on
