"""030 WS-G4 (light): a syntax gate over the ~13k LOC of webview frontend JS.

The frontend has no JS test rig (adding one means a node_modules dependency
tree the repo deliberately avoids), but nothing even PARSED the files in CI —
a stray brace shipped silently. Parse every static/js file as an ES module
with the system node. Skipped when node is unavailable.
"""
import pathlib
import shutil
import subprocess

import pytest

_JS_DIR = pathlib.Path(__file__).resolve().parents[3] / "webview" / "static" / "js"
_NODE = shutil.which("node")

_FILES = sorted(p for p in _JS_DIR.rglob("*.js") if "vendor" not in p.parts
                and ".min." not in p.name)


@pytest.mark.skipif(_NODE is None, reason="node not installed")
@pytest.mark.parametrize("path", _FILES, ids=lambda p: str(p.relative_to(_JS_DIR)))
def test_js_file_parses_as_es_module(path, tmp_path):
    # node --check treats .js as CJS (import fails); a .mjs copy parses as ESM.
    mjs = tmp_path / (path.stem + ".mjs")
    mjs.write_bytes(path.read_bytes())
    proc = subprocess.run([_NODE, "--check", str(mjs)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, (
        f"{path.relative_to(_JS_DIR)} failed to parse:\n{proc.stderr[:2000]}")


def test_the_scan_found_the_frontend():
    assert len(_FILES) >= 10, f"scanner found only {len(_FILES)} JS files"
