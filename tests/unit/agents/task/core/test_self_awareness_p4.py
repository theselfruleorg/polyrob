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


def test_env_block_shows_clamped_mode_both_axes_and_tools(monkeypatch):
    from agents.task.agent.core import env_context

    monkeypatch.setattr(env_context, "_host_capabilities", lambda: "none")
    out = env_context.build_environment_context(
        "sess-1", "u1", tool_ids=["browser", "filesystem"])
    assert out is not None
    # Clamped display, not the raw env value: with AUTONOMY_MODE unset this is
    # exactly "supervised" (the raw call would also say supervised — pin the
    # clamped path by requesting autonomous WITHOUT the owner guard satisfied).
    assert "autonomy posture" in out
    assert "Tools loaded this session: browser, filesystem." in out

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
