"""Phase 3 (path-concerns upgrade): pm() singleton must be resettable.

E1: set_path_manager mutates the module-global _INSTANCE with no reset hook, so
an in-process build_cli_container leaks a project-scoped pm() into every later
pm() caller. reset_path_manager() restores lazy-default behavior; an autouse
conftest fixture uses it for test isolation.
"""
import agents.task.path as path_module
from agents.task.path import (
    PathManager,
    pm,
    set_path_manager,
    reset_path_manager,
)


def test_reset_restores_lazy_default(tmp_path):
    custom = PathManager(data_root=str(tmp_path / "custom"))
    set_path_manager(custom)
    assert pm() is custom

    reset_path_manager()
    assert path_module._INSTANCE is None
    fresh = pm()
    assert fresh is not custom
    assert isinstance(fresh, PathManager)


def test_reset_is_idempotent():
    reset_path_manager()
    reset_path_manager()
    assert path_module._INSTANCE is None
