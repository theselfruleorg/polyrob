"""Persist the frozen avatar into the instance home (modules/pfp/store.py).

Idempotent ("created once"); falls back to a committed reference PNG when Chromium
is unavailable (so the feature works headless / in CI). CI-safe: the Playwright
renderer is monkeypatched.
"""
import json

import pytest

from modules.pfp import store
from modules.pfp.renderer import PfpRenderUnavailable, RenderResult
from core.instance import pfp_path, pfp_dir, load_pfp_meta

CFG = {"generator": "mindprint@v2", "seed": "Rob Ottmachin", "variant": "",
       "size": 256, "override": {}}
PNG = b"\x89PNG\r\n\x1a\n" + b"fakepngbytes"


def _fake_render(calls):
    def _r(config, out_png, *, size=None):
        calls.append(1)
        from pathlib import Path
        Path(out_png).parent.mkdir(parents=True, exist_ok=True)
        Path(out_png).write_bytes(PNG)
        return RenderResult(path=Path(out_png), traits={"eyes": "square"},
                            seed_hex="0x1546", voice={"pitch": 1.0})
    return _r


def test_generate_writes_png_and_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "render_still", _fake_render([]))
    meta = store.generate_pfp(tmp_path, "rob", config=CFG)
    assert pfp_path(tmp_path, "rob").read_bytes() == PNG
    assert meta["rendered_by"] == "playwright-chromium"
    assert meta["seed_hex"] == "0x1546"
    assert set(meta["voice"]) == {"pitch", "rate", "timbre"}   # from mesh, engine-agnostic
    assert meta["traits"]["eyes"] == "square"
    assert (pfp_dir(tmp_path, "rob") / "pfp.json").is_file()


def test_generate_is_idempotent(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(store, "render_still", _fake_render(calls))
    store.generate_pfp(tmp_path, "rob", config=CFG)
    store.generate_pfp(tmp_path, "rob", config=CFG)   # second call: no-op
    assert len(calls) == 1


def test_generate_force_rewrites(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(store, "render_still", _fake_render(calls))
    store.generate_pfp(tmp_path, "rob", config=CFG)
    store.generate_pfp(tmp_path, "rob", config=CFG, force=True)
    assert len(calls) == 2


def test_generate_falls_back_to_reference(tmp_path, monkeypatch):
    def _raise(*a, **k):
        raise PfpRenderUnavailable("no chromium")
    monkeypatch.setattr(store, "render_still", _raise)
    ref = tmp_path / "ref.png"
    ref.write_bytes(PNG)
    meta = store.generate_pfp(tmp_path, "rob", config=CFG, reference_png=ref)
    assert pfp_path(tmp_path, "rob").read_bytes() == PNG
    assert meta["rendered_by"] == "committed-reference"
    # traits/voice still populated (computed by the Python mesh, no browser)
    assert set(meta["voice"]) == {"pitch", "rate", "timbre"}
    assert "eyes" in meta["traits"]


def test_generate_raises_when_no_render_and_no_reference(tmp_path, monkeypatch):
    def _raise(*a, **k):
        raise PfpRenderUnavailable("no chromium")
    monkeypatch.setattr(store, "render_still", _raise)
    with pytest.raises(PfpRenderUnavailable):
        store.generate_pfp(tmp_path, "rob", config=CFG, reference_png=tmp_path / "missing.png")
