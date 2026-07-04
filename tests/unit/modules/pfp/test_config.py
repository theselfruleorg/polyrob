"""Frozen avatar config load + normalization (modules/pfp/config.py).

The config is the reproducibility SSOT: {generator, seed, variant, size, override,
traits, voice}. Two engine gotchas this module owns:
  - render seed = seed + variant (concatenated), then hashed.
  - override.shape is stored as a NAME but the engine consumes an INDEX.
"""
import json

import pytest

from modules.pfp.config import (
    load_frozen_config,
    render_seed,
    normalized_override,
    FrozenConfigError,
    SHAPES,
)


def _cfg(**over):
    base = {"generator": "mindprint@v2", "seed": "Rob Ottmachin", "variant": "",
            "size": 768, "override": {}}
    base.update(over)
    return base


def test_load_accepts_valid_config():
    assert load_frozen_config(_cfg())["seed"] == "Rob Ottmachin"


def test_load_reads_from_file(tmp_path):
    p = tmp_path / "rob.json"
    p.write_text(json.dumps(_cfg()), encoding="utf-8")
    assert load_frozen_config(p)["generator"] == "mindprint@v2"


def test_load_rejects_unknown_generator():
    with pytest.raises(FrozenConfigError):
        load_frozen_config(_cfg(generator="someengine@v9"))


def test_load_requires_nonempty_seed():
    with pytest.raises(FrozenConfigError):
        load_frozen_config(_cfg(seed=""))


def test_load_rejects_non_dict():
    with pytest.raises(FrozenConfigError):
        load_frozen_config(["not", "a", "dict"])


def test_render_seed_concatenates_variant():
    assert render_seed(_cfg(seed="Rob Ottmachin", variant="#k3j9x")) == "Rob Ottmachin#k3j9x"


def test_render_seed_without_variant():
    assert render_seed(_cfg(seed="Rob Ottmachin", variant="")) == "Rob Ottmachin"
    cfg = _cfg(); cfg.pop("variant")
    assert render_seed(cfg) == "Rob Ottmachin"


def test_normalize_override_maps_shape_name_to_index():
    out = normalized_override({"shape": "square"})
    assert out["shape"] == SHAPES.index("square")  # engine wants the INDEX


def test_normalize_override_passes_through_known_keys():
    ov = {"color": "#56e2c2", "mode": "solid", "dens": 42, "grain": 0.0,
          "voice": {"pitch": 1.1}}
    assert normalized_override(ov) == ov


def test_normalize_override_drops_unknown_shape():
    # an invalid shape name degrades to auto (omitted), never a bad index
    assert "shape" not in normalized_override({"shape": "octagon"})
