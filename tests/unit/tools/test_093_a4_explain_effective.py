"""093 A4: `preferences explain <FLAG>` carries the gate's effective line."""

import pytest

import tools  # noqa: F401  (registers the gates and the explain probe)
from tools.code_exec import backend_probe


@pytest.fixture(autouse=True)
def _fresh_probe(monkeypatch):
    for k in ("CODE_EXEC_BACKEND", "DOCKER_HOST", "SHELL_BACKEND", "CODE_EXEC_SSH_HOST",
              "CODE_EXEC_DOCKER_BINARY"):
        monkeypatch.delenv(k, raising=False)
    backend_probe.reset_probe_cache()
    yield
    backend_probe.reset_probe_cache()


class _Container:
    def __init__(self, services):
        self._s = services

    def has_service(self, name):
        return name in self._s

    def get_service(self, name):
        return self._s[name]


# -- A4: the effective line -------------------------------------------------------

def test_every_flag_a_tool_gate_names_prints_an_effective_line():
    """Ratchet (093 A4): explain(<gate flag>) carries the gate's effective state."""
    from core import config_service
    from core.tool_capabilities import gate_flag_map
    missing = []
    for flag in sorted(set(gate_flag_map().values())):
        try:
            info = config_service.explain(flag)
        except KeyError:
            missing.append(f"{flag}: not describable")
            continue
        if not info.gate_effective.startswith("effective: "):
            missing.append(f"{flag}: {info.gate_effective!r}")
    assert missing == []


def test_a_flag_no_gate_names_has_no_effective_line():
    from core import config_service
    assert config_service.explain("WEB_FETCH_ALLOW_PRIVATE_URLS").gate_effective == ""


def test_flag_on_but_no_mcp_server_is_effective_off(monkeypatch):
    """The incident: MCP_ENABLED=True, servers={} — 'MCP is already on' was false."""
    from core.tool_gates import remedy_text
    from tools import gate_effective

    class _Mcp:
        def availability_gap(self):
            return "no-mcp-server", remedy_text("no-mcp-server")

    monkeypatch.setattr(gate_effective, "_container", lambda: _Container({"mcp": _Mcp()}))
    line = gate_effective.flag_effective("MCP_ENABLED", True)
    assert line.startswith("effective: OFF — no-mcp-server: MCP_ENABLED is on, but no MCP "
                           "server is configured")


def test_explain_renderers_show_the_effective_line(monkeypatch):
    from core import config_service
    monkeypatch.setattr(config_service, "_GATE_EFFECTIVE_PROBE",
                        lambda key, value: "effective: OFF — test")
    info = config_service.explain("MCP_ENABLED")
    assert info.gate_effective == "effective: OFF — test"
    from click.testing import CliRunner
    from cli.commands.config import explain_cmd
    out = CliRunner().invoke(explain_cmd, ["MCP_ENABLED"]).output
    assert out.splitlines()[0].endswith(" · effective: OFF — test")
