"""tools/coding/snapshot.py + its wiring in tools/coding/tool.py (I-4 / H2,
dedup decision D3): off-workspace shadow-git per-file snapshot/restore.

Two sections:
- Pure-module tests exercise a REAL tmp git repo (skip-if-git-absent guard) —
  no tool coupling, no mocking of git itself.
- Tool-level tests spy/monkeypatch ``tools.coding.snapshot`` and ``pm()`` to
  verify the wiring (enablement gate, before-the-write ordering, the two new
  actions) without needing a real git binary.
"""
import logging
import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest

import agents.task.agent.service  # noqa: F401 — avoid controller import cycle
import agents.task.constants as c
from tools.coding.snapshot import list_snapshots, restore_file, snapshot_file
from tools.coding.tool import (
    CodingTool, CreateFileParams, DeleteFileParams, MoveFileParams,
    RestoreParams, SnapshotsParams, StrReplaceParams,
)

_HAS_GIT = shutil.which("git") is not None
requires_git = pytest.mark.skipif(not _HAS_GIT, reason="git not installed")


# ==========================================================================
# Pure-module tests — real tmp git repo
# ==========================================================================


@pytest.fixture()
def snap_layout(tmp_path):
    """A workspace dir + a SIBLING (off-workspace) snap dir."""
    workspace = tmp_path / "workspace"
    snap_dir = tmp_path / "coding_snapshots"
    workspace.mkdir()
    return workspace, snap_dir


@requires_git
def test_snapshot_edit_restore_roundtrip(snap_layout):
    workspace, snap_dir = snap_layout
    target = workspace / "file.txt"
    other = workspace / "other.txt"
    target.write_text("original content\n")
    other.write_text("unrelated file\n")

    sha = snapshot_file(str(snap_dir), str(workspace), "file.txt")
    assert sha

    target.write_text("MUTATED — a bad edit\n")
    ok = restore_file(str(snap_dir), str(workspace), "file.txt")
    assert ok is True
    assert target.read_text() == "original content\n"
    # A file never snapshotted (outside the touched-file set) is never touched.
    assert other.read_text() == "unrelated file\n"


@requires_git
def test_snapshot_dir_lives_outside_workspace_no_git_entry(snap_layout):
    workspace, snap_dir = snap_layout
    (workspace / "file.txt").write_text("v1\n")

    sha = snapshot_file(str(snap_dir), str(workspace), "file.txt")
    assert sha
    # The shadow git-dir is under snap_dir, never under the workspace.
    assert os.path.isdir(os.path.join(str(snap_dir), "git"))
    assert ".git" not in os.listdir(str(workspace))
    assert sorted(os.listdir(str(workspace))) == ["file.txt"]


@requires_git
def test_restore_with_explicit_older_snapshot_id(snap_layout):
    workspace, snap_dir = snap_layout
    target = workspace / "file.txt"

    target.write_text("v1\n")
    sha1 = snapshot_file(str(snap_dir), str(workspace), "file.txt")
    target.write_text("v2\n")
    sha2 = snapshot_file(str(snap_dir), str(workspace), "file.txt")
    target.write_text("v3\n")
    sha3 = snapshot_file(str(snap_dir), str(workspace), "file.txt")
    assert len({sha1, sha2, sha3}) == 3

    ok = restore_file(str(snap_dir), str(workspace), "file.txt", snapshot_id=sha1)
    assert ok is True
    assert target.read_text() == "v1\n"

    ok = restore_file(str(snap_dir), str(workspace), "file.txt", snapshot_id=sha2)
    assert ok is True
    assert target.read_text() == "v2\n"


@requires_git
def test_snapshot_unchanged_file_returns_current_head(snap_layout):
    workspace, snap_dir = snap_layout
    target = workspace / "file.txt"
    target.write_text("stable\n")

    sha1 = snapshot_file(str(snap_dir), str(workspace), "file.txt")
    sha2 = snapshot_file(str(snap_dir), str(workspace), "file.txt")  # no change since
    assert sha1 and sha2
    assert sha1 == sha2  # still a valid (redundant) restore point


@requires_git
def test_list_snapshots_newest_first(snap_layout):
    workspace, snap_dir = snap_layout
    target = workspace / "file.txt"

    target.write_text("v1\n")
    sha1 = snapshot_file(str(snap_dir), str(workspace), "file.txt")
    target.write_text("v2\n")
    sha2 = snapshot_file(str(snap_dir), str(workspace), "file.txt")

    entries = list_snapshots(str(snap_dir), str(workspace), "file.txt")
    assert [e["id"] for e in entries] == [sha2, sha1]
    for e in entries:
        assert set(e.keys()) == {"id", "date", "subject"}
        assert "file.txt" in e["subject"]


@requires_git
def test_list_snapshots_all_files_when_rel_path_omitted(snap_layout):
    workspace, snap_dir = snap_layout
    (workspace / "a.txt").write_text("a\n")
    (workspace / "b.txt").write_text("b\n")
    snapshot_file(str(snap_dir), str(workspace), "a.txt")
    snapshot_file(str(snap_dir), str(workspace), "b.txt")

    entries = list_snapshots(str(snap_dir), str(workspace))
    assert len(entries) == 2
    only_a = list_snapshots(str(snap_dir), str(workspace), "a.txt")
    assert len(only_a) == 1


def test_git_unavailable_snapshot_returns_none(tmp_path, monkeypatch):
    """Not gated on requires_git — this is exactly the "git absent" branch."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("v1\n")
    snap_dir = tmp_path / "snap"

    def _boom(*a, **k):
        raise FileNotFoundError("git executable not found on PATH")

    monkeypatch.setattr("tools.coding.snapshot.subprocess.run", _boom)

    assert snapshot_file(str(snap_dir), str(workspace), "file.txt") is None
    assert restore_file(str(snap_dir), str(workspace), "file.txt") is False
    assert list_snapshots(str(snap_dir), str(workspace)) == []


def test_git_timeout_snapshot_returns_none(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("v1\n")
    snap_dir = tmp_path / "snap"

    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=10)

    monkeypatch.setattr("tools.coding.snapshot.subprocess.run", _timeout)

    assert snapshot_file(str(snap_dir), str(workspace), "file.txt") is None
    assert restore_file(str(snap_dir), str(workspace), "file.txt") is False
    assert list_snapshots(str(snap_dir), str(workspace)) == []


@requires_git
def test_restore_no_snapshot_returns_false(snap_layout):
    workspace, snap_dir = snap_layout
    (workspace / "file.txt").write_text("v1\n")
    # Never snapshotted -> nothing to restore.
    assert restore_file(str(snap_dir), str(workspace), "file.txt") is False


@requires_git
def test_snapshot_missing_file_returns_none(snap_layout):
    workspace, snap_dir = snap_layout
    # rel_path doesn't exist on disk -> `git add` fails -> None, no raise.
    assert snapshot_file(str(snap_dir), str(workspace), "nope.txt") is None


# ==========================================================================
# Tool-level tests — flag/posture gate, before-the-write wiring, actions
# ==========================================================================


def _tool(root):
    t = object.__new__(CodingTool)
    t.logger = logging.getLogger("coding-snapshot-test")
    t._root_override = str(root)
    t._backend = None
    return t


def _owner_ctx(session_id="s1"):
    return SimpleNamespace(
        session_id=session_id, role="orchestrator", is_sub_agent=False,
        user_id="rob", metadata={},
    )


@pytest.fixture(autouse=True)
def _clean_posture(monkeypatch):
    for k in ("AGENT_COMPUTE_POSTURE", "CODING_SNAPSHOT_ENABLED", "POLYROB_OWNER_USER_ID"):
        monkeypatch.delenv(k, raising=False)
    c._refreeze_compute_posture_for_tests()
    yield
    c._refreeze_compute_posture_for_tests()


def _enable(monkeypatch):
    monkeypatch.setenv("CODING_SNAPSHOT_ENABLED", "true")
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "1")
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    c._refreeze_compute_posture_for_tests()


def _fake_pm(monkeypatch, subdir_path):
    """Stub agents.task.path.pm() so the tool never touches the real path manager."""
    calls = []

    class _FakePM:
        def get_subdir(self, session_id, subdir_name, user_id=None):
            calls.append((session_id, subdir_name, user_id))
            os.makedirs(subdir_path, exist_ok=True)
            return subdir_path

    monkeypatch.setattr("agents.task.path.pm", lambda: _FakePM())
    return calls


# --- flag OFF: byte-identical, zero pm()/git touches -------------------------

@pytest.mark.asyncio
async def test_flag_off_no_snapshot_dir_created_byte_identical(tmp_path, monkeypatch):
    def _boom():
        raise AssertionError("pm() must never be called when CODING_SNAPSHOT_ENABLED is off")

    monkeypatch.setattr("agents.task.path.pm", _boom)
    (tmp_path / "x.py").write_text("a = 1\n")
    t = _tool(tmp_path)
    res = await t.str_replace(
        StrReplaceParams(file_path="x.py", old_string="a = 1", new_string="a = 2"),
        execution_context=_owner_ctx(),
    )
    assert getattr(res, "error", None) in (None, "")
    assert res.extracted_content == "Edited x.py (1 replacement)."
    assert (tmp_path / "x.py").read_text() == "a = 2\n"


@pytest.mark.asyncio
async def test_flag_off_snapshots_action_returns_disabled_error(tmp_path):
    t = _tool(tmp_path)
    res = await t.snapshots(SnapshotsParams(), execution_context=_owner_ctx())
    assert res.error and "disabled" in res.error.lower()


@pytest.mark.asyncio
async def test_flag_off_restore_action_returns_disabled_error(tmp_path):
    t = _tool(tmp_path)
    res = await t.restore(RestoreParams(file_path="x.py"), execution_context=_owner_ctx())
    assert res.error and "disabled" in res.error.lower()


# --- flag ON: pre-mutation snapshot fires before the write --------------------

@pytest.mark.asyncio
async def test_flag_on_snapshot_called_before_str_replace_mutation(tmp_path, monkeypatch):
    _enable(monkeypatch)
    snap_dir = tmp_path / "snaps"
    _fake_pm(monkeypatch, snap_dir)

    seen_content_at_snapshot = {}

    def _spy_snapshot(dir_, workspace, rel_path):
        # Must be called while the ON-DISK file STILL has the pre-edit content.
        seen_content_at_snapshot["content"] = open(os.path.join(workspace, rel_path)).read()
        return "deadbeef"

    monkeypatch.setattr("tools.coding.snapshot.snapshot_file", _spy_snapshot)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "x.py").write_text("a = 1\n")
    t = _tool(workspace)
    res = await t.str_replace(
        StrReplaceParams(file_path="x.py", old_string="a = 1", new_string="a = 2"),
        execution_context=_owner_ctx(),
    )
    assert getattr(res, "error", None) in (None, "")
    assert seen_content_at_snapshot["content"] == "a = 1\n"  # pre-edit content
    assert (workspace / "x.py").read_text() == "a = 2\n"  # edit still applied


@pytest.mark.asyncio
async def test_flag_on_delete_file_snapshots_before_removal(tmp_path, monkeypatch):
    _enable(monkeypatch)
    _fake_pm(monkeypatch, tmp_path / "snaps")
    calls = []
    monkeypatch.setattr(
        "tools.coding.snapshot.snapshot_file",
        lambda d, w, r: calls.append(r) or "sha1",
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "gone.py").write_text("bye\n")
    t = _tool(workspace)
    res = await t.delete_file(DeleteFileParams(file_path="gone.py"), execution_context=_owner_ctx())
    assert getattr(res, "error", None) in (None, "")
    assert calls == ["gone.py"]
    assert not (workspace / "gone.py").exists()


@pytest.mark.asyncio
async def test_flag_on_create_file_skips_snapshot_for_new_file(tmp_path, monkeypatch):
    """A not-yet-existing file has nothing to restore to -> no-op skip."""
    _enable(monkeypatch)
    _fake_pm(monkeypatch, tmp_path / "snaps")
    calls = []
    monkeypatch.setattr(
        "tools.coding.snapshot.snapshot_file",
        lambda d, w, r: calls.append(r) or "sha1",
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    t = _tool(workspace)
    res = await t.create_file(
        CreateFileParams(file_path="new.py", content="x=1\n"), execution_context=_owner_ctx()
    )
    assert getattr(res, "error", None) in (None, "")
    assert calls == []  # no snapshot for a brand-new file


@pytest.mark.asyncio
async def test_flag_on_create_file_snapshots_when_overwriting(tmp_path, monkeypatch):
    _enable(monkeypatch)
    _fake_pm(monkeypatch, tmp_path / "snaps")
    calls = []
    monkeypatch.setattr(
        "tools.coding.snapshot.snapshot_file",
        lambda d, w, r: calls.append(r) or "sha1",
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "existing.py").write_text("old\n")
    t = _tool(workspace)
    res = await t.create_file(
        CreateFileParams(file_path="existing.py", content="new\n", overwrite=True),
        execution_context=_owner_ctx(),
    )
    assert getattr(res, "error", None) in (None, "")
    assert calls == ["existing.py"]


@pytest.mark.asyncio
async def test_flag_on_move_file_snapshots_source_not_missing_dest(tmp_path, monkeypatch):
    """move_file always snapshots the SOURCE; a non-existent dest is NOT snapshotted."""
    _enable(monkeypatch)
    _fake_pm(monkeypatch, tmp_path / "snaps")
    calls = []
    monkeypatch.setattr(
        "tools.coding.snapshot.snapshot_file",
        lambda d, w, r: calls.append(r) or "sha1",
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "src.py").write_text("body\n")
    t = _tool(workspace)
    res = await t.move_file(
        MoveFileParams(src_path="src.py", dest_path="dest.py"), execution_context=_owner_ctx()
    )
    assert getattr(res, "error", None) in (None, "")
    assert calls == ["src.py"]  # source only — dest didn't exist
    assert not (workspace / "src.py").exists()
    assert (workspace / "dest.py").read_text() == "body\n"


@pytest.mark.asyncio
async def test_flag_on_move_file_also_snapshots_overwritten_dest(tmp_path, monkeypatch):
    """When the move overwrites an existing dest, BOTH src and dest are snapshotted."""
    _enable(monkeypatch)
    _fake_pm(monkeypatch, tmp_path / "snaps")
    calls = []
    monkeypatch.setattr(
        "tools.coding.snapshot.snapshot_file",
        lambda d, w, r: calls.append(r) or "sha1",
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "src.py").write_text("new body\n")
    (workspace / "dest.py").write_text("about to be clobbered\n")
    t = _tool(workspace)
    res = await t.move_file(
        MoveFileParams(src_path="src.py", dest_path="dest.py", overwrite=True),
        execution_context=_owner_ctx(),
    )
    assert getattr(res, "error", None) in (None, "")
    assert calls == ["src.py", "dest.py"]  # src always, dest because it existed
    assert (workspace / "dest.py").read_text() == "new body\n"


@pytest.mark.asyncio
async def test_flag_on_but_no_session_id_skips_snapshot(tmp_path, monkeypatch):
    """compute_posture_allows passes but there's no resolvable session_id."""
    _enable(monkeypatch)

    def _boom():
        raise AssertionError("pm() must never be called without a resolvable session_id")

    monkeypatch.setattr("agents.task.path.pm", _boom)
    (tmp_path / "x.py").write_text("a = 1\n")
    t = _tool(tmp_path)
    ctx = SimpleNamespace(
        session_id=None, role="orchestrator", is_sub_agent=False, user_id="rob", metadata={},
    )
    res = await t.str_replace(
        StrReplaceParams(file_path="x.py", old_string="a = 1", new_string="a = 2"),
        execution_context=ctx,
    )
    assert getattr(res, "error", None) in (None, "")
    assert (tmp_path / "x.py").read_text() == "a = 2\n"


# --- snapshots / restore actions ----------------------------------------------

@pytest.mark.asyncio
async def test_snapshots_action_lists_formatted_entries(tmp_path, monkeypatch):
    _enable(monkeypatch)
    _fake_pm(monkeypatch, tmp_path / "snaps")
    monkeypatch.setattr(
        "tools.coding.snapshot.list_snapshots",
        lambda d, w, rel=None: [
            {"id": "abc123", "date": "2026-07-10T00:00:00Z", "subject": "pre-edit x.py"},
        ],
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "x.py").write_text("a\n")
    t = _tool(workspace)
    res = await t.snapshots(SnapshotsParams(file_path="x.py"), execution_context=_owner_ctx())
    assert getattr(res, "error", None) in (None, "")
    assert "abc123" in res.extracted_content
    assert "pre-edit x.py" in res.extracted_content


@pytest.mark.asyncio
async def test_snapshots_action_no_snapshots(tmp_path, monkeypatch):
    _enable(monkeypatch)
    _fake_pm(monkeypatch, tmp_path / "snaps")
    monkeypatch.setattr("tools.coding.snapshot.list_snapshots", lambda d, w, rel=None: [])
    workspace = tmp_path / "ws"
    workspace.mkdir()
    t = _tool(workspace)
    res = await t.snapshots(SnapshotsParams(), execution_context=_owner_ctx())
    assert getattr(res, "error", None) in (None, "")
    assert "no snapshots" in res.extracted_content.lower()


@pytest.mark.asyncio
async def test_restore_action_restores_file(tmp_path, monkeypatch):
    _enable(monkeypatch)
    _fake_pm(monkeypatch, tmp_path / "snaps")
    calls = []

    def _spy_restore(d, w, rel, snapshot_id=None):
        calls.append((rel, snapshot_id))
        return True

    monkeypatch.setattr("tools.coding.snapshot.restore_file", _spy_restore)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "x.py").write_text("current\n")
    t = _tool(workspace)
    res = await t.restore(
        RestoreParams(file_path="x.py", snapshot_id="deadbeef"), execution_context=_owner_ctx()
    )
    assert getattr(res, "error", None) in (None, "")
    assert "Restored x.py" in res.extracted_content
    assert calls == [("x.py", "deadbeef")]


@pytest.mark.asyncio
async def test_restore_action_no_snapshot_available(tmp_path, monkeypatch):
    _enable(monkeypatch)
    _fake_pm(monkeypatch, tmp_path / "snaps")
    monkeypatch.setattr("tools.coding.snapshot.restore_file", lambda d, w, rel, snapshot_id=None: False)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "x.py").write_text("current\n")
    t = _tool(workspace)
    res = await t.restore(RestoreParams(file_path="x.py"), execution_context=_owner_ctx())
    assert res.error and "no snapshot" in res.error.lower()


@pytest.mark.asyncio
async def test_restore_action_path_escape_blocked(tmp_path, monkeypatch):
    _enable(monkeypatch)
    _fake_pm(monkeypatch, tmp_path / "snaps")
    t = _tool(tmp_path)
    res = await t.restore(RestoreParams(file_path="../../etc/passwd"), execution_context=_owner_ctx())
    assert res.error and "escape" in res.error.lower()
