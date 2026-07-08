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


# --- SA-08: the two confirmed-divergent bool parsers now use the core.env SSOT ---

def test_mcp_enabled_parses_identically_in_eip8004_and_config(monkeypatch):
    """MCP_ENABLED=1 must be True in BOTH core/config (pydantic bool) and the eip8004
    agent-card registration — the old `== "true"` made =1 disagree (card said disabled
    while MCP was running)."""
    monkeypatch.setenv("MCP_ENABLED", "1")
    assert env.bool_env("MCP_ENABLED", False) is True
    from core.config import BotConfig
    assert BotConfig().mcp_enabled is True
    # the eip8004 MCP_ENABLED read must not reintroduce the ad-hoc comparison
    import inspect
    import modules.eip8004.registration as reg
    src = inspect.getsource(reg)
    assert 'os.environ.get("MCP_ENABLED", "false").lower() == "true"' not in src
    assert 'bool_env("MCP_ENABLED"' in src

def test_auto_agent_init_uses_ssot_parser():
    import inspect
    import agents.task.agent as agent_pkg
    src = inspect.getsource(agent_pkg)
    assert "('1', 'true', 'yes')" not in src  # ad-hoc truthy-set removed
    assert "bool_env" in src
