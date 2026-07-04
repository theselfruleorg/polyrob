import importlib, inspect
import core.env as env
import agents.task.constants as c
import core.config as cfg

def test_core_env_has_value_based_parse_bool():
    assert hasattr(env, "parse_bool")
    assert env.parse_bool("off", True) is False
    assert env.parse_bool("", True) is False       # "" is falsey (value-based)
    assert env.parse_bool(None, True) is True       # None -> default

def test_bool_env_blank_is_default(monkeypatch):
    monkeypatch.delenv("ZZZ_UNSET", raising=False)
    assert env.bool_env("ZZZ_UNSET", True) is True   # blank/unset -> default (preserved)

def test_constants_bool_env_delegates_to_core_env():
    src = inspect.getsource(c._bool_env)
    assert "bool_env" in src   # delegates to core.env.bool_env, not reimplemented

def test_env_parse_module_deleted():
    try:
        importlib.import_module("core.env_parse")
        assert False, "core.env_parse must be deleted (folded into core.env)"
    except ModuleNotFoundError:
        pass

def test_coerce_memory_flag_removed():
    assert not hasattr(cfg, "_coerce_memory_flag")

def test_memory_flag_behavior_preserved(monkeypatch):
    monkeypatch.setenv("HIERARCHICAL_MEMORY_ENABLED", "off")
    from core.config import BotConfig
    assert BotConfig().HIERARCHICAL_MEMORY_ENABLED is False
