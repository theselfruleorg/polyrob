"""Discovery through REAL distribution metadata, not an injected EntryPoint
(067 P2). The cheap form writes the ``.dist-info`` an installer would; the slow
form ``pip install``s the fixture into a throwaway venv (skipped when this
interpreter cannot build a wheel offline)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]
ECHO = REPO / "tests" / "fixtures" / "packs" / "echo"

_PROBE = """
import json
from core.packs.loader import load_packs
from core.packs.loader import _FIRST_PARTY
_FIRST_PARTY["echo"] = ("polyrob-echo", "polyrob_echo:pack")  # reviewed test fixture
from core.packs import state
load_packs()
rec = state.record("echo")
print(json.dumps({"status": rec and rec.status, "reason": rec and rec.reason,
                  "records": [r.id for r in state.records()]}))
"""


def _probe(python: str, extra_path: list) -> dict:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([*extra_path, str(REPO)])}
    env.pop("POLYROB_PACKS", None)
    env.pop("POLYROB_PACKS_DISABLED", None)
    out = subprocess.run([python, "-c", _PROBE], cwd=str(REPO), env=env,
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr[-3000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_the_entry_point_group_is_read_from_dist_info(tmp_path):
    site = tmp_path / "site"
    info = site / "polyrob_echo-0.1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: polyrob-echo\nVersion: 0.1.0\n")
    (info / "entry_points.txt").write_text("[polyrob.packs]\necho = polyrob_echo:pack\n")
    got = _probe(sys.executable, [str(site), str(ECHO)])
    assert got["status"] == "loaded", got


@pytest.mark.slow
def test_pip_install_into_a_venv(tmp_path):
    pytest.importorskip("wheel", reason="no offline wheel builder in this interpreter")
    venv = tmp_path / "venv"
    # Reuse the available offline builder. ensurepip otherwise installs an old
    # bundled setuptools into the child, shadowing the parent's compatible one
    # (new wheel releases no longer supply setuptools' bdist_wheel command).
    subprocess.run([sys.executable, "-m", "venv", "--system-site-packages",
                    "--without-pip", str(venv)],
                   check=True, timeout=60)
    python = str(venv / "bin" / "python")
    subprocess.run([python, "-m", "pip", "install", "-q", "--no-deps", "--no-build-isolation",
                    "--no-index", str(ECHO)], check=True, timeout=60)
    got = _probe(python, [])
    assert got["status"] == "loaded", got
