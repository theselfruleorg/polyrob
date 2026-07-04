import importlib
import agents.task.constants as C


def test_untrusted_wrap_unknown_truthy_stays_on(monkeypatch):
    monkeypatch.setenv("UNTRUSTED_TOOL_RESULT_WRAP", "enabled")
    importlib.reload(C)
    assert C.UNTRUSTED_TOOL_RESULT_WRAP is True  # falsey-set SSOT: only none/off/false/0/no/'' disable


def test_untrusted_wrap_off_values(monkeypatch):
    monkeypatch.setenv("UNTRUSTED_TOOL_RESULT_WRAP", "off")
    importlib.reload(C)
    assert C.UNTRUSTED_TOOL_RESULT_WRAP is False


def test_progressive_disclosure_access_time(monkeypatch):
    importlib.reload(C)
    monkeypatch.setenv("SKILL_PROGRESSIVE_DISCLOSURE", "off")
    assert C.skill_progressive_disclosure() is False
    monkeypatch.setenv("SKILL_PROGRESSIVE_DISCLOSURE", "yes")
    assert C.skill_progressive_disclosure() is True


def test_decoy_constants_removed():
    importlib.reload(C)
    assert not hasattr(C, "MEMORY_PREFETCH_CADENCE")   # live gate is memory_prefetch_cadence()
    assert not hasattr(C, "TASK_PERSONALITY_BLOCK")    # live gate is task_personality_block_enabled()
