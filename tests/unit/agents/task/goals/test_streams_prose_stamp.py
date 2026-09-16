"""The manifest prose stamp must describe prose the row ACTUALLY holds.

`manifest_prose_hash` means "the manifest version this row last followed", and
`_sync_prose` uses it to tell a manifest edit (apply it) from an owner edit
(never overwrite it). That only works if the stamp advances exactly when the
prose does.

It did not. `declared_objective_payload` stamped the CURRENT manifest hash
unconditionally, and `_sync_declared_payload` wrote it on every run — including
the adopt-by-title path, which never calls `_sync_prose` at all. So a legacy
objective adopted by title got the new manifest's stamp while keeping its old
body, and from that moment `row_hash != stored` read as "the owner edited this",
freezing the row against every future manifest edit. Nobody had edited anything.

Found by reading the code, not from an incident — prod's live stream objectives
were checked on 2026-09-10 and their stamps were consistent. The exposure is any
objective that enters through ADOPTION rather than creation, which is the
documented migration path for a stream whose objective predates its manifest
entry; the shipped manifest says so in as many words for the standing missions.
"""
import pytest
import yaml

from agents.task.goals.board import GoalBoard
from agents.task.goals import streams as S


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


def _manifest(tmp_path, body: str, *, title: str = "Demo objective"):
    doc = {
        "version": 1,
        "streams": [{
            "id": "demo-stream",
            "cadence_hours": 4,
            "max_live_goals": 2,
            "objective": {
                "title": title,
                "body": body,
                "priority": 1,
                "success_criteria": "a file exists",
                "goal_budget": 12,
            },
            "goals": [{
                "title": "Demo goal",
                "body": "Do it.",
                "priority": 2,
                "tools": ["task"],
                "max_steps": 5,
            }],
        }],
    }
    p = tmp_path / "streams.yaml"
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return S.load_manifest(str(p))[0]


def _stamp(board, oid):
    return board.get(oid).payload.get("manifest_prose_hash")


def test_adopted_legacy_objective_is_not_stamped_with_prose_it_does_not_hold(
        board, tmp_path):
    """The adopt path must not claim the row follows a manifest it does not."""
    legacy = board.create_objective(
        user_id="rob", title="Demo objective",
        body="The OLD legacy body nobody has migrated.", force=True)

    stream = _manifest(tmp_path, "The manifest body, version one.")
    oid, note = S.ensure_objective(board, "rob", stream)
    assert oid == legacy.id and "adopted" in note

    row = board.get(oid)
    assert row.body == "The OLD legacy body nobody has migrated.", (
        "adoption must not silently rewrite prose")
    stamp = row.payload.get("manifest_prose_hash")
    assert stamp != S._prose_hash("Demo objective", "The manifest body, version one."), (
        "the row was stamped with a manifest version whose prose it does not "
        "hold — that is the freeze bug")


def test_an_adopted_row_still_follows_a_later_manifest_edit(board, tmp_path):
    """The whole point: adoption must leave the row reachable by the manifest."""
    board.create_objective(
        user_id="rob", title="Demo objective",
        body="The OLD legacy body nobody has migrated.", force=True)

    stream = _manifest(tmp_path, "The manifest body, version one.")
    oid, _ = S.ensure_objective(board, "rob", stream)

    # Second tick, same manifest: the row must converge onto the manifest prose
    # rather than sitting frozen behind a stamp it never earned.
    S.ensure_objective(board, "rob", stream)
    assert board.get(oid).body == "The manifest body, version one."

    # And a genuine later manifest edit must still land.
    stream2 = _manifest(tmp_path, "The manifest body, version TWO.")
    S.ensure_objective(board, "rob", stream2)
    assert board.get(oid).body == "The manifest body, version TWO."


def test_a_failed_prose_sync_does_not_advance_the_stamp(board, tmp_path):
    """If the prose did not move, the stamp must not move either.

    This is the invariant, stated directly: the stamp names the manifest version
    the row last actually followed, so a tick where `_sync_prose` declined must
    leave it alone.

    Note what this is NOT claiming. An owner edit was already safe under the old
    code — once the row holds the owner's words, `row_hash` stops matching any
    manifest hash and the sync declines forever, whichever version the stamp
    drifted to. The harm the drift causes is on the ADOPTION path (see the two
    tests above), where the row never held the prose it was stamped with. This
    test pins the invariant that makes that fix coherent rather than a special
    case.
    """
    stream = _manifest(tmp_path, "Manifest body one.")
    oid, _ = S.ensure_objective(board, "rob", stream)
    stamp_v1 = _stamp(board, oid)
    assert stamp_v1 == S._prose_hash("Demo objective", "Manifest body one.")

    # The owner rewrites the objective in chat.
    board.update_fields(oid, body="The OWNER's own words.")

    stream2 = _manifest(tmp_path, "Manifest body two.")
    S.ensure_objective(board, "rob", stream2)

    assert board.get(oid).body == "The OWNER's own words.", (
        "an owner edit must win over the manifest")
    assert _stamp(board, oid) == stamp_v1, (
        "the stamp advanced to a manifest version whose prose this row never "
        "took, so it no longer means what _sync_prose reads it to mean")


def test_owner_edit_survives_repeated_manifest_ticks(board, tmp_path):
    """Owner-wins must survive the fix.

    This passed before the change too; it is here so the stamp rework cannot
    quietly trade the freeze bug for the worse failure in the other direction —
    a manifest tick eating the owner's words.
    """
    stream = _manifest(tmp_path, "Manifest body one.")
    oid, _ = S.ensure_objective(board, "rob", stream)
    board.update_fields(oid, body="The OWNER's own words.")

    stream2 = _manifest(tmp_path, "Manifest body two.")
    for _ in range(3):
        S.ensure_objective(board, "rob", stream2)
        assert board.get(oid).body == "The OWNER's own words."


def test_manifest_edit_still_reaches_an_unedited_row(board, tmp_path):
    """The ordinary path must keep working."""
    stream = _manifest(tmp_path, "Manifest body one.")
    oid, _ = S.ensure_objective(board, "rob", stream)
    assert board.get(oid).body == "Manifest body one."

    stream2 = _manifest(tmp_path, "Manifest body two.")
    S.ensure_objective(board, "rob", stream2)
    assert board.get(oid).body == "Manifest body two."
    assert _stamp(board, oid) == S._prose_hash("Demo objective", "Manifest body two.")
