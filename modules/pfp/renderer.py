"""Headless still renderer — runs the EXACT ``avatar/mindprint.js`` engine in
Chromium (Playwright) and writes a deterministic PNG. Python only orchestrates;
every pixel comes from the committed JS engine.

Playwright is an OPTIONAL extra (``pip install '.[browser]' && playwright install
chromium``). When it (or Chromium) is absent, :func:`render_still` raises
:class:`PfpRenderUnavailable` so callers (``store.generate_pfp``) can fall back to the
committed reference PNG. The renderer is never on a runtime/interactive path.

Engine loading: the engine is a CLASSIC script (window globals, no ES-module export),
injected via ``set_content`` on ``about:blank`` — no ``file://`` / ESM / CSP concerns
and no local HTTP server.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .config import render_seed, normalized_override

_AVATAR = Path(__file__).resolve().parents[2] / "avatar"
_ENGINE = _AVATAR / "mindprint.js"

# Renders a deterministic STILL and returns the PNG data-URL + derived meta.
_JS_RENDER = """(a) => {
  const mp = new Mindprint(a.seed);
  Object.assign(mp.override, a.override || {});
  const cv = document.getElementById('c');
  cv.width = cv.height = a.size;
  mp.N = 0;
  mp.render(cv.getContext('2d'), a.size, 1.0, 0, {still: true});
  mp.N = 0;
  return {dataURL: cv.toDataURL('image/png'), traits: mp.traitList(),
          hex: mp.hex, voice: mp.voiceCfg()};
}"""


class PfpRenderUnavailable(RuntimeError):
    """Raised when Playwright/Chromium is not available to render a still."""


@dataclass
class RenderResult:
    path: Path
    traits: Dict[str, Any]
    seed_hex: str
    voice: Dict[str, float]


def render_still(config: Dict[str, Any], out_png, *, size: Optional[int] = None) -> RenderResult:
    """Render ``config`` to ``out_png`` via headless Chromium. Raises
    :class:`PfpRenderUnavailable` if Playwright/Chromium is missing."""
    try:
        from playwright.sync_api import sync_playwright, Error as PlaywrightError
    except ImportError as e:  # extra not installed
        raise PfpRenderUnavailable("playwright not installed ('.[browser]')") from e

    engine = _ENGINE.read_text(encoding="utf-8")
    seed = render_seed(config)
    override = normalized_override(config.get("override", {}) or {})
    sz = int(size or config.get("size") or 768)
    html = (
        "<!doctype html><meta charset='utf-8'>"
        "<canvas id='c'></canvas><script>" + engine + "</script>"
    )

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--disable-gpu"])
            try:
                page = browser.new_page()
                page.set_content(html, wait_until="load")
                res = page.evaluate(_JS_RENDER, {"seed": seed, "override": override, "size": sz})
            finally:
                browser.close()
    except PlaywrightError as e:  # chromium binary missing / launch failure
        raise PfpRenderUnavailable(f"chromium unavailable: {e}") from e

    data = base64.b64decode(res["dataURL"].split(",", 1)[1])
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    out_png.write_bytes(data)
    return RenderResult(path=out_png, traits=res["traits"], seed_hex=res["hex"], voice=res["voice"])
