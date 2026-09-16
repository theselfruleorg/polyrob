"""030 WS-G4 (light): a syntax gate over the ~13k LOC of webview frontend JS.

The frontend has no JS test rig (adding one means a node_modules dependency
tree the repo deliberately avoids), but nothing even PARSED the files in CI —
a stray brace shipped silently. Parse every static JS file as an ES module
with the system node. Skipped when node is unavailable.

Two roots since 043 C1: the legacy ``static/js`` and the new ``static/app``,
which is where the 043 interface lives. ``static/app`` may hold no JS at all
(the console is still stack-free HTML + CSS) — an empty root is fine, an
UNSCANNED one is not, which is what ``test_every_js_root_is_scanned`` pins.

Note this is the *Python* gate, and it runs in the normal suite with no npm
install. The richer dev-only rig (vitest + jsdom + axe-core over a real
browser) lives in ``webview/dev/`` and runs in the ``console`` workflow.
"""
import pathlib
import shutil
import subprocess

import pytest

_STATIC = pathlib.Path(__file__).resolve().parents[3] / "webview" / "static"
_ROOTS = (_STATIC / "js", _STATIC / "app")
_NODE = shutil.which("node")


def _scan(root: pathlib.Path):
    if not root.is_dir():
        return []
    return [p for p in root.rglob("*.js")
            if "vendor" not in p.parts and ".min." not in p.name]


_FILES = sorted(f for root in _ROOTS for f in _scan(root))


@pytest.mark.skipif(_NODE is None, reason="node not installed")
@pytest.mark.parametrize("path", _FILES, ids=lambda p: str(p.relative_to(_STATIC)))
def test_js_file_parses_as_es_module(path, tmp_path):
    # node --check treats .js as CJS (import fails); a .mjs copy parses as ESM.
    mjs = tmp_path / (path.stem + ".mjs")
    mjs.write_bytes(path.read_bytes())
    proc = subprocess.run([_NODE, "--check", str(mjs)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, (
        f"{path.relative_to(_STATIC)} failed to parse:\n{proc.stderr[:2000]}")


def test_the_scan_found_the_frontend():
    assert len(_FILES) >= 10, f"scanner found only {len(_FILES)} JS files"


def test_every_js_root_is_scanned():
    """A new JS directory under ``static/`` must join ``_ROOTS``.

    The failure this catches is silent: a whole directory of unparsed JS that
    the gate reports as green because it never looked at it.
    """
    scanned = {r.resolve() for r in _ROOTS}
    unscanned = sorted(
        d.name for d in _STATIC.iterdir()
        if d.is_dir() and d.resolve() not in scanned and any(d.rglob("*.js"))
    )
    assert not unscanned, (
        f"webview/static/{unscanned} holds JS that nothing parses — add it to _ROOTS")
