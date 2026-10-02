"""The discovery pack registers the anysite live gate in the loader's phase 2
(067 P3a; it was ``tools/anysite`` at import before)."""
import subprocess
import sys
from unittest import mock

import pytest

pytest.importorskip("polyrob_discovery")

from core import tool_gates  # noqa: E402


def test_the_gate_registers_in_phase_two_not_at_import_tools():
    code = (
        "import tools\n"
        "from core.tool_gates import gate_for\n"
        "assert gate_for('anysite') is None, 'core must not register a pack gate'\n"
        "from core.packs.loader import load_packs\n"
        "load_packs()\n"
        "assert gate_for('anysite') is not None\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_anysite_gate_follows_the_flag(monkeypatch):
    monkeypatch.delenv("ANYSITE_TOOL_ENABLED", raising=False)
    assert tool_gates.gate_on("anysite") is True
    monkeypatch.setenv("ANYSITE_TOOL_ENABLED", "false")
    assert tool_gates.gate_on("anysite") is False


def test_patching_the_tool_function_still_takes_effect():
    with mock.patch("polyrob_discovery.anysite.anysite_cli_enabled", return_value=False):
        assert tool_gates.gate_on("anysite") is False
    with mock.patch("polyrob_discovery.anysite.anysite_cli_enabled", return_value=True):
        assert tool_gates.gate_on("anysite") is True
