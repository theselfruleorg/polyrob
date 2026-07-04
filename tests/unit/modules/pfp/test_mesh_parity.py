"""JS<->Python parity for the mesh field port (modules/pfp/mesh.py vs avatar/mindprint.js).

The deterministic core MUST match the SSOT engine exactly: cyrb128 hex, every
_genParams field (traits, geometry, voice, hue/sat), the blink timing, and the fbm/n2
noise. Runs the real engine under Node (skipped where node is unavailable).
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from modules.pfp.mesh import Mesh

ENGINE = Path(__file__).resolve().parents[4] / "avatar" / "mindprint.js"
SEEDS = ["Rob Ottmachin", "Ada Nine", "polyrob", "Kilo Prime", "Nyx Orbital", "Pixel"]

_NUMERIC = [
    "headExp", "headRx", "headRy", "eyeSep", "eyeY", "eyeR", "eyeBright",
    "winkSide", "cheekX", "cheekY", "cheekStr", "mouthY", "mouthW", "smile",
    "mouthBright", "antX", "antTop", "antSpread", "asymX", "hue", "sat",
    "densAuto", "grainAuto", "shapeIdx",
]
_STRINGS = ["tier", "headType", "eyeStyle", "brow", "mouth", "ant", "aura", "mode"]

_HARNESS = r"""
const fs=require("fs");
const src=fs.readFileSync(process.argv[1],"utf8");
const seeds=JSON.parse(process.argv[2]);
const run=(seed)=>{
  const mp=eval(src+"\n;new Mindprint("+JSON.stringify(seed)+")");
  const p=mp.p;
  const keep={};
  for(const k of ["tier","headType","headExp","headRx","headRy","eyeSep","eyeY","eyeR",
    "eyeBright","eyeStyle","winkSide","brow","cheekX","cheekY","cheekStr","mouth","mouthY",
    "mouthW","smile","mouthBright","ant","antX","antTop","antSpread","asymX","aura","hue",
    "sat","mode","densAuto","grainAuto","shapeIdx"]) keep[k]=p[k];
  keep.voice=p.voice;
  return {hex:mp.hex, p:keep, blinkPhase:mp.blinkPhase, blinkEvery:mp.blinkEvery,
          fbm:[mp._fbm(1.5,2.5), mp._fbm(0.31,0.77)], n2:[mp._n2(3,5), mp._n2(40,13)]};
};
process.stdout.write(JSON.stringify(seeds.map(run)));
"""

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def _js_all():
    res = subprocess.run(
        ["node", "-e", _HARNESS, str(ENGINE), json.dumps(SEEDS)],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, f"node failed: {res.stderr}"
    return json.loads(res.stdout)


def _mesh(seed):
    return Mesh({"generator": "mindprint@v2", "seed": seed, "variant": "", "override": {}})


def test_cyrb128_hex_parity():
    js = _js_all()
    for seed, j in zip(SEEDS, js):
        assert _mesh(seed).hex == j["hex"], f"hex mismatch for {seed!r}"


def test_genparams_strings_parity():
    js = _js_all()
    for seed, j in zip(SEEDS, js):
        p = _mesh(seed).p
        for k in _STRINGS:
            assert p[k] == j["p"][k], f"{k} mismatch for {seed!r}: py={p[k]} js={j['p'][k]}"


def test_genparams_numeric_parity():
    js = _js_all()
    for seed, j in zip(SEEDS, js):
        p = _mesh(seed).p
        for k in _NUMERIC:
            assert p[k] == pytest.approx(j["p"][k], abs=1e-9), \
                f"{k} mismatch for {seed!r}: py={p[k]} js={j['p'][k]}"


def test_voice_parity():
    js = _js_all()
    for seed, j in zip(SEEDS, js):
        v, jv = _mesh(seed).p["voice"], j["p"]["voice"]
        for k in ("pitch", "rate", "timbre"):
            assert v[k] == pytest.approx(jv[k], abs=1e-9), f"voice.{k} mismatch for {seed!r}"


def test_blink_and_noise_parity():
    js = _js_all()
    for seed, j in zip(SEEDS, js):
        m = _mesh(seed)
        assert m.blink_phase == pytest.approx(j["blinkPhase"], abs=1e-9)
        assert m.blink_every == pytest.approx(j["blinkEvery"], abs=1e-9)
        assert m._fbm(1.5, 2.5) == pytest.approx(j["fbm"][0], abs=1e-9)
        assert m._fbm(0.31, 0.77) == pytest.approx(j["fbm"][1], abs=1e-9)
        assert m._n2(3, 5) == pytest.approx(j["n2"][0], abs=1e-9)
        assert m._n2(40, 13) == pytest.approx(j["n2"][1], abs=1e-9)
