"""Persist the frozen avatar into the instance home (modules/pfp/store.py).

Idempotent ("created once"). Render chain: Chromium (exact engine) → native Pillow
mesh renderer (same face, no browser) → committed reference PNG (STOCK identity only —
for any other config the reference pixels would contradict the recorded traits).
CI-safe: both renderers are monkeypatched where the chain itself is under test.
"""
import json

import pytest

from modules.pfp import store
import modules.pfp.still as still_mod
from modules.pfp.renderer import PfpRenderUnavailable, RenderResult
from core.instance import pfp_path, pfp_dir, load_pfp_meta

CFG = {"generator": "mindprint@v2", "seed": "Rob Ottmachin", "variant": "",
       "size": 256, "override": {}}
RANDOM_CFG = {**CFG, "variant": "#zq9k3"}
PNG = b"\x89PNG\r\n\x1a\n" + b"fakepngbytes"


def _raise_unavailable(*a, **k):
    raise PfpRenderUnavailable("no chromium")


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


def test_generate_falls_back_to_pillow_mesh(tmp_path, monkeypatch):
    # Chromium unavailable -> the native mesh renderer produces a REAL png that
    # matches the config (this is what makes a randomized identity honest headless).
    monkeypatch.setattr(store, "render_still", _raise_unavailable)
    meta = store.generate_pfp(tmp_path, "rob", config=RANDOM_CFG)
    png = pfp_path(tmp_path, "rob")
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert meta["rendered_by"] == "pillow-mesh"
    assert set(meta["voice"]) == {"pitch", "rate", "timbre"}
    assert "eyes" in meta["traits"]


def test_generate_falls_back_to_reference(tmp_path, monkeypatch):
    # Both renderers down -> an explicitly-supplied reference PNG is honored.
    monkeypatch.setattr(store, "render_still", _raise_unavailable)
    monkeypatch.setattr(still_mod, "render_still_mesh", _raise_unavailable)
    ref = tmp_path / "ref.png"
    ref.write_bytes(PNG)
    meta = store.generate_pfp(tmp_path, "rob", config=CFG, reference_png=ref)
    assert pfp_path(tmp_path, "rob").read_bytes() == PNG
    assert meta["rendered_by"] == "committed-reference"
    # traits/voice still populated (computed by the Python mesh, no browser)
    assert set(meta["voice"]) == {"pitch", "rate", "timbre"}
    assert "eyes" in meta["traits"]


def test_default_reference_never_masks_a_random_identity(tmp_path, monkeypatch):
    # Both renderers down + NO explicit reference: the committed default reference
    # (one fixed face) must NOT be substituted for a non-stock config — fail honestly.
    monkeypatch.setattr(store, "render_still", _raise_unavailable)
    monkeypatch.setattr(still_mod, "render_still_mesh", _raise_unavailable)
    with pytest.raises(PfpRenderUnavailable):
        store.generate_pfp(tmp_path, "rob", config=RANDOM_CFG)


def test_generate_raises_when_no_render_and_no_reference(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "render_still", _raise_unavailable)
    monkeypatch.setattr(still_mod, "render_still_mesh", _raise_unavailable)
    with pytest.raises(PfpRenderUnavailable):
        store.generate_pfp(tmp_path, "rob", config=CFG, reference_png=tmp_path / "missing.png")


# ---- one-time setup contract (draft -> keep -> immutable) ----

def test_fresh_mint_is_a_draft(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "render_still", _fake_render([]))
    meta = store.generate_pfp(tmp_path, "rob", config=CFG)
    assert meta["locked"] is False                      # setup still open


def test_keep_locks_the_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "render_still", _fake_render([]))
    store.generate_pfp(tmp_path, "rob", config=CFG)
    kept = store.keep_pfp(tmp_path, "rob")
    assert kept["locked"] is True and kept["kept_at"]
    assert load_pfp_meta(tmp_path, "rob")["locked"] is True


def test_keep_requires_an_avatar(tmp_path):
    with pytest.raises(FileNotFoundError):
        store.keep_pfp(tmp_path, "rob")


def test_locked_identity_cannot_be_changed(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "render_still", _fake_render([]))
    store.generate_pfp(tmp_path, "rob", config=CFG)
    store.keep_pfp(tmp_path, "rob")
    with pytest.raises(store.PfpLockedError):
        store.generate_pfp(tmp_path, "rob", config=RANDOM_CFG, force=True)


def test_locked_identity_can_rerender_its_own_pixels(tmp_path, monkeypatch):
    # e.g. the browser extra got installed later: same identity, better pixels — OK
    calls = []
    monkeypatch.setattr(store, "render_still", _fake_render(calls))
    store.generate_pfp(tmp_path, "rob", config=CFG)
    store.keep_pfp(tmp_path, "rob")
    meta = store.generate_pfp(tmp_path, "rob", config=CFG, force=True)
    assert len(calls) == 2
    assert meta["locked"] is True                       # stays kept
    assert meta["kept_at"]                              # acceptance timestamp survives


def test_draft_can_be_rerolled(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "render_still", _fake_render([]))
    store.generate_pfp(tmp_path, "rob", config=CFG)     # draft
    meta = store.generate_pfp(tmp_path, "rob", config=RANDOM_CFG, force=True)
    assert meta["variant"] == "#zq9k3" and meta["locked"] is False


def test_legacy_meta_without_locked_key_is_locked(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "render_still", _fake_render([]))
    store.generate_pfp(tmp_path, "rob", config=CFG)
    meta_path = pfp_dir(tmp_path, "rob") / "pfp.json"
    legacy = json.loads(meta_path.read_text())
    legacy.pop("locked")                                # pre-lock-era meta
    meta_path.write_text(json.dumps(legacy))
    assert store.is_locked(load_pfp_meta(tmp_path, "rob")) is True
    with pytest.raises(store.PfpLockedError):
        store.generate_pfp(tmp_path, "rob", config=RANDOM_CFG, force=True)
