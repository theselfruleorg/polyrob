"""MT-4/MT-5: goal in-flight concurrency is single-flight on a shared project folder.

Concurrent goal runs in project-root mode would interleave read-modify-write edits
on the SAME files (the battle-test "read INDEX.md, append" corruption). Clamp to 1 —
but keyed off the installed pm() (NOT an env var), so the multi-tenant server (whose
global pm() is per-session) keeps full GOAL_MAX_CONCURRENT even if POLYROB_PROJECT_DIR
is set in its env.
"""

import pytest


@pytest.fixture(autouse=True)
def _reset_pm():
    from agents.task.path import reset_path_manager
    reset_path_manager()
    yield
    reset_path_manager()


def _install_pm(tmp_path, project_root):
    from agents.task.path import get_path_manager, set_path_manager
    pm = get_path_manager(
        data_root=str(tmp_path / "d"),
        workspace_is_project_root=project_root is not None,
        project_root=project_root,
    )
    set_path_manager(pm)


def test_clamps_to_one_in_project_root_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("GOAL_MAX_CONCURRENT", "5")
    _install_pm(tmp_path, str(tmp_path / "proj"))
    from agents.task.goals.dispatcher import effective_goal_concurrency
    assert effective_goal_concurrency() == 1


def test_unclamped_for_default_per_session_pm(tmp_path, monkeypatch):
    # Server-equivalent: default per-session pm() => NOT clamped even with the env set.
    monkeypatch.setenv("GOAL_MAX_CONCURRENT", "5")
    monkeypatch.setenv("POLYROB_PROJECT_DIR", str(tmp_path / "proj"))  # MT-5: must be ignored here
    _install_pm(tmp_path, None)
    from agents.task.goals.dispatcher import effective_goal_concurrency
    assert effective_goal_concurrency() == 5
