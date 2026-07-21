"""Native (no-browser) still renderer (modules/pfp/still.py).

The Pillow/numpy port of the engine dot pass: real PNGs, deterministic per config,
distinct across variants, honoring the resolved dot shape. No Chromium anywhere.
"""
from PIL import Image

from modules.pfp.still import render_still_mesh
from modules.pfp.mesh import Mesh

BASE = {"generator": "mindprint@v2", "seed": "Rob Ottmachin", "variant": "",
        "size": 192, "override": {}}


def _cfg(**kw):
    cfg = dict(BASE)
    cfg.update(kw)
    return cfg


def test_renders_a_valid_png_of_the_requested_size(tmp_path):
    out = tmp_path / "a.png"
    res = render_still_mesh(_cfg(), out, size=192)
    img = Image.open(out)
    assert img.size == (192, 192)
    assert res.path == out
    assert set(res.voice) == {"pitch", "rate", "timbre"}
    assert "eyes" in res.traits


def test_same_config_is_deterministic(tmp_path):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    render_still_mesh(_cfg(variant="#det"), a)
    render_still_mesh(_cfg(variant="#det"), b)
    assert a.read_bytes() == b.read_bytes()


def test_different_variants_render_different_faces(tmp_path):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    render_still_mesh(_cfg(variant="#one"), a)
    render_still_mesh(_cfg(variant="#two"), b)
    assert a.read_bytes() != b.read_bytes()


def test_meta_matches_the_mesh_for_the_same_config(tmp_path):
    cfg = _cfg(variant="#meta")
    res = render_still_mesh(cfg, tmp_path / "m.png")
    mesh = Mesh(cfg)
    assert res.traits == mesh.traits()
    assert res.voice == mesh.voice()
    assert res.seed_hex == mesh.hex


def test_all_dot_shapes_render(tmp_path):
    for shape in ("dot", "square", "scanline"):
        out = tmp_path / f"{shape}.png"
        render_still_mesh(_cfg(override={"shape": shape}), out)
        assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_resolved_cfg_exposes_dens_and_shape():
    mesh = Mesh(_cfg())
    cfg = mesh.render_cfg
    assert cfg["shape"] in ("dot", "square", "scanline")
    assert isinstance(cfg["dens"], int) and cfg["dens"] >= 8
    # override wins
    mesh2 = Mesh(_cfg(override={"shape": "scanline", "dens": 40}))
    assert mesh2.render_cfg["shape"] == "scanline"
    assert mesh2.render_cfg["dens"] == 40
