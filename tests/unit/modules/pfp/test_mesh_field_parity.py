"""JS<->Python FIELD parity — the strong guard that the PORT renders the SAME FACE,
not just the same params. Captures per-cell luminance (globalAlpha=v01) from the REAL
avatar/mindprint.js render() via a fake canvas ctx in Node, and compares to mesh._lum
sampled at the same cell centres. Catches formula-transcription errors params-parity
can't. Skipped where node is unavailable."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from modules.pfp.mesh import Mesh, clamp

ENGINE = Path(__file__).resolve().parents[4] / "avatar" / "mindprint.js"
SEEDS = ["Rob Ottmachin", "polyrob", "Nyx Orbital", "Kilo Prime"]

# Capture v01 per drawn cell from the real still render (dens pinned for a small grid).
_HARNESS = r"""
const fs=require("fs");
const src=fs.readFileSync(process.argv[1],"utf8");
const seeds=JSON.parse(process.argv[2]);
function capture(seed){
  const mp=eval(src+"\n;(function(){const m=new Mindprint(seed);m.override.dens=42;return m;})()");
  const N=42, size=420, cell=size/N;
  const grid={}; let lx=0, ly=0;
  function rec(cx,cy,a){ const i=Math.round(cx/cell-0.5), j=Math.round(cy/cell-0.5);
    if(i<0||j<0||i>=N||j>=N)return; const k=i+","+j; grid[k]=Math.max(grid[k]||0,a); }
  const ctx={ globalCompositeOperation:"", fillStyle:"", globalAlpha:1,
    createRadialGradient:()=>({addColorStop:()=>{}}),
    fillRect:(x,y,w,h)=>{ if(w>=size*0.9)return; rec(x+w/2,y+h/2,ctx.globalAlpha); },
    beginPath:()=>{}, arc:(x,y)=>{ lx=x; ly=y; }, fill:()=>{ rec(lx,ly,ctx.globalAlpha); } };
  mp.N=0; mp.render(ctx,size,1.0,0,{still:true}); mp.N=0;
  return {N, grid};
}
process.stdout.write(JSON.stringify(seeds.map(capture)));
"""

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def _js_capture():
    res = subprocess.run(["node", "-e", _HARNESS, str(ENGINE), json.dumps(SEEDS)],
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_field_luminance_matches_js_render():
    caps = _js_capture()
    for seed, cap in zip(SEEDS, caps):
        N = cap["N"]
        mesh = Mesh({"generator": "mindprint@v2", "seed": seed, "variant": "",
                     "override": {"dens": 42}})
        worst = 0.0
        for key, js_v01 in cap["grid"].items():
            i, j = map(int, key.split(","))
            Xb = (i + 0.5) / N - 0.5
            Yb = (j + 0.5) / N - 0.5
            lum, _dh = mesh._lum(Xb, Yb, 1.0, True)  # still frame, glow=1 (amp 0)
            py_v01 = clamp(lum, 0, 1)
            worst = max(worst, abs(py_v01 - js_v01))
        assert worst < 1e-6, f"{seed!r}: max per-cell luminance delta {worst} (formula transcription drift)"
