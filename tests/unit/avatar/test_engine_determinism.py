"""Drift guard for the committed avatar engine `avatar/mindprint.js`.

The engine is the SSOT for pixels (studio, logo, headless still, webview). A silent
edit that changes existing faces is a brand/identity regression, so we pin the
deterministic output for a canonical seed. Runs the EXACT committed JS under Node
(skipped where node is unavailable). The stronger JS<->Python field parity guard
lands in Phase C (mesh.py).
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[3] / "avatar" / "mindprint.js"

# Pinned baseline captured from the extracted v2 engine (seed "Rob Ottmachin").
# If the engine legitimately changes, bump generator -> mindprint@vN and re-pin here.
EXPECTED_HEX = "0x1546"
EXPECTED_TRAITS = {
    "tier": "basic", "eyes": "square", "brow": "none", "mouth": "grin",
    "antenna": "single", "aura": "none", "head": "orb", "mode": "solid",
    "voice": {"pitch": 1.29, "rate": 1.02, "timbre": 0.78},
}

_HARNESS = (
    'const fs=require("fs");'
    'const src=fs.readFileSync(process.argv[1],"utf8");'
    'const o=eval(src+"\\n;({'
    "hex:new Mindprint('Rob Ottmachin').hex,"
    "traits:new Mindprint('Rob Ottmachin').traitList(),"
    "syms:{Mindprint:typeof Mindprint,cyrb128:typeof cyrb128,sfc32:typeof sfc32,"
    "TIERS:typeof TIERS,TRAITS:typeof TRAITS,SHAPES:typeof SHAPES,MODES:typeof MODES}"
    '})");'
    'process.stdout.write(JSON.stringify(o));'
)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def _run_engine():
    assert ENGINE.is_file(), f"missing committed engine: {ENGINE}"
    res = subprocess.run(
        ["node", "-e", _HARNESS, str(ENGINE)],
        capture_output=True, text=True, timeout=30,
    )
    assert res.returncode == 0, f"node failed: {res.stderr}"
    return json.loads(res.stdout)


def test_engine_exports_expected_symbols():
    syms = _run_engine()["syms"]
    assert syms["Mindprint"] == "function"
    assert syms["cyrb128"] == "function"
    assert syms["sfc32"] == "function"
    for k in ("TIERS", "TRAITS", "SHAPES", "MODES"):
        assert syms[k] == "object", f"{k} missing/renamed"


def test_engine_seed_hex_is_stable():
    assert _run_engine()["hex"] == EXPECTED_HEX


def test_engine_traits_and_voice_are_stable():
    assert _run_engine()["traits"] == EXPECTED_TRAITS
