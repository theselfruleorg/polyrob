"""Pure-Python behaviour of the mesh field renderer (no browser/node needed)."""
from modules.pfp.mesh import Mesh


def _cfg(seed="Rob Ottmachin", **over):
    return {"generator": "mindprint@v2", "seed": seed, "variant": "", "override": over}


def _lum(cell):
    r, g, b = cell
    return 0.299 * r + 0.587 * g + 0.114 * b


def test_grid_dimensions():
    g = Mesh(_cfg()).grid(56, 28, still=True)
    assert len(g) == 28 and all(len(row) == 56 for row in g)


def test_grid_is_deterministic():
    a = Mesh(_cfg()).grid(40, 40, t=1.0, still=True)
    b = Mesh(_cfg()).grid(40, 40, t=1.0, still=True)
    assert a == b


def test_face_is_brighter_than_corners():
    g = Mesh(_cfg()).grid(40, 40, still=True)
    corners = _lum(g[0][0]) + _lum(g[0][-1]) + _lum(g[-1][0]) + _lum(g[-1][-1])
    center = _lum(g[20][20]) + _lum(g[18][18]) + _lum(g[22][22])
    assert center > corners  # a face exists in the middle, corners are empty


def test_traits_and_voice_have_expected_shape():
    m = Mesh(_cfg())
    t = m.traits()
    assert set(t) == {"tier", "eyes", "brow", "mouth", "antenna", "aura", "head", "mode"}
    v = m.voice()
    assert set(v) == {"pitch", "rate", "timbre"}


def test_color_override_changes_pixels():
    green = Mesh(_cfg(color="#56e2c2")).grid(40, 40, still=True)
    rose = Mesh(_cfg(color="#ff92c2")).grid(40, 40, still=True)
    assert green != rose  # same face geometry, different hue


def test_voice_override_is_applied():
    m = Mesh(_cfg(voice={"pitch": 1.5}))
    assert m.voice()["pitch"] == 1.5
