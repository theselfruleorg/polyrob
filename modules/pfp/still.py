"""Native (no-browser) still renderer — the Mindprint dot pass in Pillow + numpy.

``renderer.py`` (Playwright/Chromium, the EXACT JS engine) stays the high-fidelity
path. This module is the honest headless fallback: it renders the SAME FACE from the
parity-tested Python field port (:class:`modules.pfp.mesh.Mesh`) using the same dot
pass as ``avatar/mindprint.js`` ``render()`` — N×N cells, additive ("lighter") dots
with per-dot alpha, the >0.62 glow halo, and the radial aura wash. It exists so a
RANDOMIZED identity always materializes with pixels that match its own traits, instead
of silently falling back to the committed reference face (which is ONE fixed identity).

Not byte-identical to the canvas (no canvas antialiaser here) — same geometry, same
traits, same palette: the same face. Pillow and numpy are hard deps of the project.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .mesh import Mesh, clamp, hsl2rgb
from .renderer import RenderResult


def _add_circle(buf, cx: float, cy: float, rad: float, rgb, alpha: float) -> None:
    """Additively draw a feathered disc onto the float buffer (canvas 'lighter')."""
    import numpy as np

    size = buf.shape[0]
    x0 = max(0, int(cx - rad - 1))
    x1 = min(size, int(cx + rad + 2))
    y0 = max(0, int(cy - rad - 1))
    y1 = min(size, int(cy + rad + 2))
    if x0 >= x1 or y0 >= y1:
        return
    yy, xx = np.ogrid[y0:y1, x0:x1]
    dist = np.sqrt((xx + 0.5 - cx) ** 2 + (yy + 0.5 - cy) ** 2)
    cov = np.clip(rad + 0.5 - dist, 0.0, 1.0)  # ~1px feathered edge
    tile = cov[:, :, None] * (np.asarray(rgb, dtype=np.float32) * alpha)
    buf[y0:y1, x0:x1, :] += tile


def _add_rect(buf, cx: float, cy: float, w: float, h: float, rgb, alpha: float) -> None:
    """Additively draw a feathered axis-aligned rect onto the float buffer."""
    import numpy as np

    size = buf.shape[0]
    x0 = max(0, int(cx - w / 2 - 1))
    x1 = min(size, int(cx + w / 2 + 2))
    y0 = max(0, int(cy - h / 2 - 1))
    y1 = min(size, int(cy + h / 2 + 2))
    if x0 >= x1 or y0 >= y1:
        return
    yy, xx = np.ogrid[y0:y1, x0:x1]
    covx = np.clip(w / 2 + 0.5 - np.abs(xx + 0.5 - cx), 0.0, 1.0)
    covy = np.clip(h / 2 + 0.5 - np.abs(yy + 0.5 - cy), 0.0, 1.0)
    tile = (covx * covy)[:, :, None] * (np.asarray(rgb, dtype=np.float32) * alpha)
    buf[y0:y1, x0:x1, :] += tile


def _aura_wash(buf, hue: float, sat: float) -> None:
    """The radial aura gradient behind the dots (mirrors the JS render preamble)."""
    import numpy as np

    size = buf.shape[0]
    midc = hsl2rgb(hue, sat * 0.9, 0.5)
    cx, cy, radius = size / 2.0, size * 0.46, size * 0.52
    yy, xx = np.ogrid[0:size, 0:size]
    dist = np.sqrt((xx + 0.5 - cx) ** 2 + (yy + 0.5 - cy) ** 2)
    a = np.clip(1.0 - dist / radius, 0.0, 1.0) * 0.09
    buf += a[:, :, None] * np.asarray(midc, dtype=np.float32)


def render_still_mesh(config: Dict[str, Any], out_png, *,
                      size: Optional[int] = None) -> RenderResult:
    """Render ``config`` to ``out_png`` from the Python mesh (no browser)."""
    import numpy as np
    from PIL import Image

    mesh = Mesh(config)
    cfg = mesh.render_cfg
    n = max(8, int(cfg["dens"]))
    shape = cfg["shape"]
    sz = int(size or config.get("size") or 768)
    cell = sz / n

    buf = np.zeros((sz, sz, 3), dtype=np.float32)
    _aura_wash(buf, cfg["hue"], cfg["sat"])

    for j in range(n):
        yb = (j + 0.5) / n - 0.5
        for i in range(n):
            xb = (i + 0.5) / n - 0.5
            lum, dh = mesh._lum(xb, yb, 1.0, True)   # still frame: breath/blink off
            if lum <= 0.05:
                continue
            v01 = clamp(lum, 0, 1)
            rgb = mesh._color(lum, dh, xb, yb, True)
            cx, cy = (i + 0.5) * cell, (j + 0.5) * cell
            rad = cell * 0.5 * (0.30 + 0.85 * v01)
            if shape == "square":
                s2 = rad * 1.7
                _add_rect(buf, cx, cy, s2, s2, rgb, v01)
            elif shape == "scanline":
                _add_rect(buf, cx, cy, cell * (0.35 + 0.9 * v01),
                          max(1.0, cell * 0.42), rgb, v01)
            else:
                _add_circle(buf, cx, cy, rad, rgb, v01)
            if v01 > 0.62:  # soft glow halo, as in the JS pass
                _add_circle(buf, cx, cy, rad * 2.3, rgb, (v01 - 0.62) * 0.5)

    img = Image.fromarray(np.clip(buf, 0, 255).astype(np.uint8), mode="RGB")
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_png, format="PNG")
    return RenderResult(path=out_png, traits=mesh.traits(),
                        seed_hex=mesh.hex, voice=mesh.voice())
