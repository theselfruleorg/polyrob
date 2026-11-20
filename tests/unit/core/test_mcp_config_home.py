"""U13: the MCP settings model and the ``mcp`` registration gate live in
``core.mcp_config``. ``import core.config`` alone registers the gate, so the CLI's
``_cli_extra_gate`` never meets an unregistered (fail-open) ``mcp``."""
import os
import subprocess
import sys
import textwrap

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def test_a_fresh_import_of_core_config_registers_the_mcp_gate(tmp_path):
    env = {k: v for k, v in os.environ.items()
           if k not in ("MCP_ENABLED", "POLYROB_OVERRIDES")}
    env["HOME"] = str(tmp_path)
    env["POLYROB_HOME"] = str(tmp_path / ".polyrob")
    code = textwrap.dedent("""
        import sys
        import core.config  # noqa: F401
        from core.tool_gates import gate_for
        assert gate_for("mcp") is not None, "mcp gate not registered"
        assert "tools.mcp.config" not in sys.modules, "core.config imported the tools tier"
        print("ok")
    """)
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert "ok" in out.stdout


def test_the_old_path_is_the_same_objects():
    import core.mcp_config as new
    import tools.mcp.config as old
    for name in ("MCPConfig", "MCPServerConfig", "MCPServerType", "load_local_mcp_servers",
                 "mcp_explicitly_off", "mcp_gate_on", "get_default_mcp_config",
                 "resolve_config_environment_variables"):
        assert getattr(old, name) is getattr(new, name), name
