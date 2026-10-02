"""A removed orchestrator key in a stored config is ignored, not a load failure
(the model forbids extra keys)."""
from agents.task.config import OrchestratorConfigModel


def test_removed_key_is_ignored():
    cfg = OrchestratorConfigModel(enable_todo_forced_updates=True)
    assert not hasattr(cfg, "enable_todo_forced_updates")


def test_unknown_key_still_fails():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        OrchestratorConfigModel(not_a_real_key=True)


def test_every_mode_builds():
    for mode in ("fast", "balanced", "thorough"):
        OrchestratorConfigModel.from_mode(mode)
