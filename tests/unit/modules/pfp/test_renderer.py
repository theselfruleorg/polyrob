"""Headless still renderer (modules/pfp/renderer.py) — runs the EXACT engine in
Chromium via Playwright. Gated on the optional browser extra; skipped in CI."""
import importlib.util

import pytest


def _chromium_available() -> bool:
    """The package alone isn't enough — a fresh [all] install has playwright
    but no downloaded browser binary, and these tests must skip, not fail."""
    if importlib.util.find_spec("playwright") is None:
        return False
    try:
        from pathlib import Path

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _chromium_available(),
    reason="playwright extra or its chromium binary not installed",
)

from modules.pfp.renderer import render_still, RenderResult  # noqa: E402

CFG = {"generator": "mindprint@v2", "seed": "Rob Ottmachin", "variant": "",
       "size": 256, "override": {}}
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def test_render_writes_a_png(tmp_path):
    out = tmp_path / "pfp.png"
    res = render_still(CFG, out, size=128)
    assert isinstance(res, RenderResult)
    assert out.is_file()
    assert out.read_bytes()[:8] == PNG_MAGIC


def test_render_returns_traits_voice_hex(tmp_path):
    res = render_still(CFG, tmp_path / "p.png", size=128)
    assert res.seed_hex == "0x1546"
    assert res.traits["eyes"] == "square"      # matches the pinned baseline
    assert set(res.voice) == {"pitch", "rate", "timbre"}


def test_render_is_deterministic_same_build(tmp_path):
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    render_still(CFG, a, size=128)
    render_still(CFG, b, size=128)
    assert a.read_bytes() == b.read_bytes()   # byte-identical on the same Chromium


def test_render_applies_overrides(tmp_path):
    # override.color + shape name->index must reach the engine and change pixels
    base = tmp_path / "base.png"
    over = tmp_path / "over.png"
    render_still(CFG, base, size=128)
    cfg2 = {**CFG, "override": {"color": "#56e2c2", "mode": "solid", "shape": "square"}}
    render_still(cfg2, over, size=128)
    assert base.read_bytes() != over.read_bytes()   # recolored/reshaped face differs
