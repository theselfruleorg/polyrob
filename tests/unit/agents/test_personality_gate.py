from agents.task import constants


def test_personality_off_by_default_on_server(monkeypatch):
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("TASK_PERSONALITY_BLOCK", raising=False)
    assert constants.task_personality_block_enabled() is False


def test_personality_on_under_local_mode(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.delenv("TASK_PERSONALITY_BLOCK", raising=False)
    assert constants.task_personality_block_enabled() is True


def test_explicit_env_overrides_local_default(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("TASK_PERSONALITY_BLOCK", "false")
    assert constants.task_personality_block_enabled() is False


def test_empty_string_env_falls_through_to_local_mode(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("TASK_PERSONALITY_BLOCK", "")   # blank != "off"
    assert constants.task_personality_block_enabled() is True
