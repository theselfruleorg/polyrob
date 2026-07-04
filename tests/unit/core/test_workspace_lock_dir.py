"""MT-6: the cross-process workspace lock must live WITH the project it guards.

When project (POLYROB_PROJECT_DIR) and data home (POLYROB_DATA_DIR) are decoupled,
keying the lock off the data home gives two processes sharing one project DIFFERENT
lock files — the single-writer guarantee silently evaporates. The lock dir must
follow the project. Byte-identical for both legacy branches.
"""

from pathlib import Path

from core.bootstrap import _resolve_workspace_lock_dir


def test_lock_dir_is_data_home_when_not_project_root():
    # Headless POLYROB_DATA_DIR-only mode (per-session ephemeral): byte-identical legacy.
    data = Path("/var/lib/polyrob").resolve()
    assert _resolve_workspace_lock_dir(data, False, None) == str(data)


def test_lock_dir_cwd_default_equals_dot_polyrob():
    # cwd-default mode: project_root == cwd, data_home == cwd/.polyrob. The lock dir
    # must stay at cwd/.polyrob (today's path), NOT the bare cwd (git pollution).
    cwd = Path.cwd()
    data_home = cwd / ".polyrob"
    assert _resolve_workspace_lock_dir(data_home, True, str(cwd)) == str(cwd / ".polyrob")


def test_lock_dir_follows_project_when_data_diverges():
    # The bug: decoupled project (/b) + data (/a) must lock under the PROJECT so two
    # processes sharing /b but with different data dirs serialize on ONE lock file.
    assert _resolve_workspace_lock_dir(Path("/a"), True, "/b") == str(Path("/b") / ".polyrob")
