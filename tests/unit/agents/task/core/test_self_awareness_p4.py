"""P4 (proposal 018): the agent's self-config awareness tells the truth.

- The <environment> block renders the CLAMPED autonomy mode
  (autonomy_mode_display), both axes, and the positive loaded-tool list.
- agent_status's posture line carries mode= (clamped display).
- self_env can never patch core/config_policy/ source (the config-DEFAULT
  code deserves the same protection as config files).
"""
import pytest


@pytest.fixture(autouse=True)
def _local(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)


def test_env_block_shows_clamped_mode_both_axes_and_no_tool_list(monkeypatch):
    from agents.task.agent.core import env_context

    monkeypatch.setattr(env_context, "_host_capabilities", lambda: "none")
    out = env_context.build_environment_context("sess-1", "u1")
    assert out is not None
    # Clamped display, not the raw env value: with AUTONOMY_MODE unset this is
    # exactly "supervised" (the raw call would also say supervised — pin the
    # clamped path by requesting autonomous WITHOUT the owner guard satisfied).
    assert "autonomy posture" in out
    # 095 P0.1: the loaded tools are in <tool-catalog> ([loaded]), not here.
    assert "Tools loaded this session" not in out

    monkeypatch.setenv("AUTONOMY_MODE", "autonomous")
    monkeypatch.delenv("POLYROB_OWNER_USER_ID", raising=False)
    monkeypatch.delenv("BOT_OWNER_USER_ID", raising=False)
    from core.config_policy import reset_autonomy_mode_warnings
    reset_autonomy_mode_warnings()
    out2 = env_context.build_environment_context("sess-1", "u1")
    assert out2 is not None and "clamped" in out2  # never a bare 'autonomous' lie


def test_self_env_refuses_config_policy_source(tmp_path):
    from tools.self_env.tool import SelfEnvTool

    tool = SelfEnvTool.__new__(SelfEnvTool)
    tool._install_root = lambda: tmp_path
    (tmp_path / "core" / "config_policy").mkdir(parents=True)
    (tmp_path / "core" / "config_policy" / "policy.py").write_text("x = 1\n")
    target, err = tool._confine("core/config_policy/policy.py")
    assert target is None
    assert "capability-policy" in err
    # Ordinary source stays reachable.
    (tmp_path / "core" / "ok.py").write_text("y = 2\n")
    target2, err2 = tool._confine("core/ok.py")
    assert err2 is None and target2 is not None


def test_env_block_names_host_executables_only_when_an_exec_tool_is_open(monkeypatch):
    """112 K4: prod forces CODE_EXEC_ENABLED=false — the block must not say the host
    binaries are visible to exec tools that are closed."""
    from agents.task.agent.core import env_context, tool_availability
    monkeypatch.setattr(env_context, "_host_capabilities", lambda: "git, python3")
    monkeypatch.setattr(tool_availability, "compute_gate", lambda t: ("x", "disabled", "r"))
    assert "No exec tool is open here" in env_context._exec_tools_line()
    monkeypatch.setattr(tool_availability, "compute_gate",
                        lambda t: ("x", "loadable" if t == "shell" else "disabled", "r"))
    line = env_context._exec_tools_line()
    assert "(shell)" in line and "git, python3" in line


def test_env_block_names_the_model_tiers():
    """112 W2: the agent knows its tiers (it could not see HEAVY_MODEL before)."""
    import agents.task.constants  # noqa: F401
    from agents.task.agent.core import env_context
    env = {"DEFAULT_PROVIDER": "openrouter", "DEFAULT_MODEL": "deepseek/deepseek-v4.1-flash",
           "HEAVY_MODEL": "deepseek/deepseek-v4-pro"}
    line = env_context.model_tiers_line(env)
    assert "heavy = deepseek/deepseek-v4-pro" in line and "default = deepseek/deepseek-v4.1-flash" in line
    assert env_context.model_tiers_line({}) == ""
