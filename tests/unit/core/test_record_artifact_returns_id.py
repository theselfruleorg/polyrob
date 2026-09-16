"""043 A18 — record_artifact() returns the row id instead of None.

``core.artifacts.record_artifact`` is the tool-agnostic write-time recorder
every tool that produces a workspace file calls into
(``tools/filesystem.py``, ``tools/coding/tool.py``). It used to return
``None`` unconditionally, so a caller had no way to stamp the artifact's id
onto its own ``ActionResult`` without a SECOND lookup (or reintroducing the
deleted ``file_references`` list). ``ArtifactLedger.record`` already returns
the full ``Artifact`` row (id included) — ``record_artifact`` now surfaces
just the id (a short uuid4 hex string, per ``Artifact.id: str`` /
``artifacts.id TEXT PRIMARY KEY``), and ``None`` on every fail-open path.

The suite-wide autouse fixture ``tests.conftest._isolate_artifacts_db``
already points ``ARTIFACTS_DB_PATH`` at a per-test tmp path and resets the
module singleton, so no extra ledger isolation is needed here.
"""
from core.artifacts import record_artifact


def _write(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body)
    return str(p)


def test_record_artifact_returns_the_row_id(tmp_path):
    path = _write(tmp_path, "a.txt", "hello")

    rid = record_artifact("u1", path, session_id="s1", kind="file")

    assert isinstance(rid, str) and rid
    # The returned id resolves the SAME row via the ledger.
    from core.artifacts import get_artifact_ledger
    row = get_artifact_ledger().get(rid, "u1")
    assert row is not None
    assert row.path == path


def test_record_artifact_returns_none_without_user_id(tmp_path):
    path = _write(tmp_path, "b.txt", "hello")
    assert record_artifact(None, path) is None
    assert record_artifact("", path) is None


def test_record_artifact_returns_none_for_missing_file(tmp_path):
    missing = str(tmp_path / "does-not-exist.txt")
    assert record_artifact("u1", missing) is None


def test_record_artifact_rewrite_returns_the_same_id(tmp_path):
    # Re-recording the same (user_id, path) updates the existing row rather
    # than minting a second one — the id returned must be stable.
    path = _write(tmp_path, "c.txt", "v1")
    first = record_artifact("u1", path, kind="file")
    assert first

    with open(path, "w") as f:
        f.write("v2, a longer body")
    second = record_artifact("u1", path, kind="file")

    assert second == first
