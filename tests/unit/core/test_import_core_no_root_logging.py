"""D6 (structural review 2026-10-10): `import core` must not configure the
host's ROOT logger. A library import leaves an embedder's logging alone; the
process entry points (cli.polyrob.main, api.server_boot.run_server,
webview.server, the signer, polyrob.app.bootstrap) call setup_logging themselves."""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

_PROBE = r"""
import logging
root = logging.getLogger()
mine = logging.StreamHandler()
root.addHandler(mine)
root.setLevel(logging.WARNING + 1)
import core, core.config, core.version, core.container  # noqa: F401
from core.logging import get_component_logger
get_component_logger("task.probe")   # an import-time component logger
assert root.handlers == [mine], root.handlers
assert root.level == logging.WARNING + 1, root.level
from core.logging import setup_logging
setup_logging(log_level="INFO")      # the explicit entry-point call still configures
assert mine not in root.handlers and len(root.handlers) == 3, root.handlers
print("OK")
"""


def test_import_core_leaves_root_logger_unchanged(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
           "POLYROB_DATA_DIR": str(tmp_path / "data"), "PYTHONPATH": str(REPO)}
    proc = subprocess.run([sys.executable, "-c", _PROBE], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip().endswith("OK")


def test_entry_points_configure_logging():
    """Each process entry point carries the setup call `import core` dropped."""
    for rel in ("cli/polyrob.py", "api/server_boot.py", "webview/server.py",
                "tools/defi/signer_entry.py", "core/lazy_installer.py"):
        text = (REPO / rel).read_text()
        assert "setup_logging" in text or "entrypoint_logger" in text, rel
