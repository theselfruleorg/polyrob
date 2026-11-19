import importlib
import agents.task.constants as C


def test_unknown_truthy_stays_on(monkeypatch):
    from core.env import bool_env
    monkeypatch.setenv("FLAG_PARSER_SAMPLE", "enabled")
    assert bool_env("FLAG_PARSER_SAMPLE", True) is True  # falsey-set SSOT: only none/off/false/0/no/'' disable


def test_off_values(monkeypatch):
    from core.env import bool_env
    monkeypatch.setenv("FLAG_PARSER_SAMPLE", "off")
    assert bool_env("FLAG_PARSER_SAMPLE", True) is False


def test_decoy_constants_removed():
    importlib.reload(C)
    assert not hasattr(C, "MEMORY_PREFETCH_CADENCE")   # live gate is memory_prefetch_cadence()
    assert not hasattr(C, "TASK_PERSONALITY_BLOCK")    # live gate is task_personality_block_enabled()
